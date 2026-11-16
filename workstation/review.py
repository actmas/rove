"""Independent, memory-free, read-only /review orchestration with fail-closed freshness.

Inspired by makecindy/cindy's Review product direction:
- Never resumes the source developer session; creates an isolated read-only job.
- Omits developer reasoning/thinking and labels the developer's final reply as an
  unverified self-report so the reviewer inspects actual files and Git diffs.
- Computes workspace fingerprint (git HEAD + dirty diff hash) at review start and
  invalidates stale reviews fail-closed if the workspace changes before or after.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from . import context_bridge
from . import store as db

FINDINGS_COUNT_RE = re.compile(r'FINDINGS_COUNT\s*:\s*(\d+)', re.IGNORECASE)


def _git_output(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ['git', *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout if proc.returncode == 0 else ''
    except Exception:
        return ''


def compute_workspace_fingerprint(project_path: str) -> Tuple[str, str]:
    """Return (git_head, workspace_hash) for freshness verification."""
    root = Path(project_path)
    if not root.is_dir():
        return '', ''
    head = _git_output(root, 'rev-parse', 'HEAD').strip()
    if head:
        status = _git_output(root, 'status', '--porcelain')
        diff = _git_output(root, 'diff', 'HEAD')
        untracked_list = _git_output(root, 'ls-files', '--others', '--exclude-standard').splitlines()
        untracked_hashes = []
        for rel in sorted(untracked_list)[:200]:
            p = root / rel
            if p.is_file():
                try:
                    data = p.read_bytes()[:262144]
                    untracked_hashes.append(f'{rel}:{hashlib.sha256(data).hexdigest()[:16]}')
                except OSError:
                    untracked_hashes.append(f'{rel}:unreadable')
        combined = f'{head}\n{status}\n{diff}\n' + '\n'.join(untracked_hashes)
        digest = hashlib.sha256(combined.encode('utf-8', errors='replace')).hexdigest()[:16]
        return head, digest

    # Non-git directory fallback: hash top-level + src mtime/size snapshot (bounded)
    entries: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.venv'))
        for fn in sorted(filenames):
            if fn.startswith('.'):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
                entries.append(f'{p.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}')
            except OSError:
                continue
            if len(entries) >= 500:
                break
        if len(entries) >= 500:
            break
    digest = hashlib.sha256('\n'.join(entries).encode('utf-8')).hexdigest()[:16]
    return 'non-git', digest


def _collect_git_diff_summary(project_path: str, max_chars: int = 6000) -> str:
    root = Path(project_path)
    if not root.is_dir():
        return ''
    status = _git_output(root, 'status', '--short').strip()
    diff = _git_output(root, 'diff', 'HEAD')
    if not diff:
        diff = _git_output(root, 'diff')
    parts = []
    if status:
        parts.append('### Git Status\n```text\n' + status[:1500] + '\n```')
    if diff:
        trimmed = diff[:max_chars]
        if len(diff) > max_chars:
            trimmed += '\n... (diff truncated; inspect local files directly)'
        parts.append('### Git Diff\n```diff\n' + trimmed + '\n```')
    return '\n\n'.join(parts)


def build_review_prompt(
    session: Dict[str, Any],
    project: Dict[str, Any],
    focus: str = '',
) -> str:
    """Build an isolated, evidence-grounded review prompt without developer reasoning."""
    cwd = session.get('worktree_path') or session.get('cwd') or project.get('path') or ''
    turns = context_bridge.extract_session_turns(session['id'])
    substantive = [t for t in turns if t.get('prompt') or t.get('reply') or t.get('files')]

    user_goals = []
    changed_files = set()
    last_dev_reply = ''
    for idx, t in enumerate(substantive, 1):
        p = (t.get('prompt') or '').strip()
        if p:
            user_goals.append(f'{idx}. {p[:300]}')
        for f in (t.get('files') or ()):
            changed_files.add(str(f))
        if t.get('reply'):
            last_dev_reply = t['reply'].strip()

    diff_section = _collect_git_diff_summary(cwd) if cwd else ''

    lines = [
        '# [独立只读成果复核任务 / Independent Read-Only Review]',
        f'- **项目**: `{project.get("name", "")}` (`{cwd}`)',
        f'- **复核模式**: 独立只读审查（严禁修改任何文件；请亲自读取本地代码与 Diff 取证，不要轻信开发方自述）',
    ]
    if focus.strip():
        lines.append(f'- **重点关注**: {focus.strip()}')

    if user_goals:
        lines.extend(['', '## 1. 用户原始需求与修正要求', '\n'.join(user_goals[-6:])])

    if changed_files:
        file_list = '\n'.join(f'- `{f}`' for f in sorted(changed_files)[:20])
        lines.extend(['', '## 2. 会话记录提及的变更文件', file_list])

    if diff_section:
        lines.extend(['', '## 3. 实时工作区 Git 证据', diff_section])

    if last_dev_reply:
        lines.extend([
            '',
            '## 4. 开发方交付说明（未经核实的自述，仅供参考，务必对照实际文件核验）',
            last_dev_reply[:1600],
        ])

    lines.extend([
        '',
        '## 5. 输出要求',
        '- 按优先级列出高置信度的真实缺陷：`P0`（阻断/崩溃/安全漏洞）、`P1`（逻辑错误/边界遗漏）、`P2`（可维护性/性能）。',
        '- 每条 Finding 必须包含：`[级别] 文件路径:行号 — 问题描述与复现/证据`。',
        '- 若没有发现实质性问题，明确说明通过原因。',
        '- **必须在回答最后一行单独输出机器可读计数**：`FINDINGS_COUNT: <整数>`（例如 `FINDINGS_COUNT: 0` 或 `FINDINGS_COUNT: 2`）。',
    ])
    return '\n'.join(lines)


def parse_findings_count(output_text: str) -> Optional[int]:
    if not output_text:
        return None
    matches = FINDINGS_COUNT_RE.findall(output_text)
    if not matches:
        return None
    return int(matches[-1])


def create_review_record(
    session: Dict[str, Any],
    project: Dict[str, Any],
    reviewer_engine: str,
    reviewer_model: Optional[str],
    focus: str = '',
) -> Dict[str, Any]:
    cwd = session.get('worktree_path') or session.get('cwd') or project.get('path') or ''
    git_head, ws_hash = compute_workspace_fingerprint(cwd)
    ident = secrets.token_hex(10)
    now = time.time()
    db.execute(
        'INSERT INTO reviews(id, session_id, project_id, review_job_id, reviewer_engine, reviewer_model, '
        'git_head, workspace_hash, focus, status, findings_count, raw_output, applied, created, updated) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            ident,
            session['id'],
            project['id'],
            None,
            reviewer_engine,
            reviewer_model or '',
            git_head,
            ws_hash,
            focus,
            'running',
            None,
            '',
            0,
            now,
            now,
        ),
    )
    return db.one('SELECT * FROM reviews WHERE id=?', (ident,))


def complete_review_record(
    review_id: str,
    raw_output: str,
    project_path: str,
    failed: bool = False,
) -> Optional[Dict[str, Any]]:
    row = db.one('SELECT * FROM reviews WHERE id=?', (review_id,))
    if not row:
        return None
    _, current_hash = compute_workspace_fingerprint(project_path)
    if failed:
        status = 'failed'
    elif row['workspace_hash'] and current_hash and current_hash != row['workspace_hash']:
        status = 'stale'
    else:
        status = 'completed'
    count = parse_findings_count(raw_output)
    db.execute(
        'UPDATE reviews SET status=?, findings_count=?, raw_output=?, updated=? WHERE id=?',
        (status, count, raw_output, time.time(), review_id),
    )
    return db.one('SELECT * FROM reviews WHERE id=?', (review_id,))


def check_review_freshness(review_row: Dict[str, Any], project_path: str) -> Dict[str, Any]:
    """Fail-closed freshness check: mark completed review stale if workspace changed or unreadable."""
    if not review_row or review_row.get('status') != 'completed':
        return review_row
    _, current_hash = compute_workspace_fingerprint(project_path)
    if not current_hash or (review_row.get('workspace_hash') and current_hash != review_row['workspace_hash']):
        db.execute("UPDATE reviews SET status='stale', updated=? WHERE id=?", (time.time(), review_row['id']))
        review_row = dict(review_row)
        review_row['status'] = 'stale'
    return review_row


def build_fix_prompt_from_review(review_row: Dict[str, Any]) -> str:
    """Build a structured handoff prompt to send validated findings back to source session."""
    return (
        '以下是独立只读 Reviewer 对当前工作区变更的复核结论。请逐条对照当前代码核实：\n'
        '- 属实的问题请直接修复并补充验证；\n'
        '- 若存在误报，请给出具体代码依据说明原因。\n\n'
        f'{review_row.get("raw_output", "").strip()}'
    )
