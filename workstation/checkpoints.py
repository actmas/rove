"""Hidden Git Ref checkpoints for per-turn workspace snapshots, turn-scoped diffs, and co-ordinated revert."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Dict, List

from . import store as db

SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{4,64}$')


def _git(args: List[str], cwd: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ['git', '-c', 'core.quotepath=false', *args],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        check=check,
        timeout=25,
    )


def _is_git_repo_with_head(workdir: str) -> bool:
    if not workdir or not Path(workdir).is_dir():
        return False
    res = _git(['rev-parse', '--is-inside-work-tree'], workdir, check=False)
    if res.returncode != 0 or res.stdout.strip() != 'true':
        return False
    head = _git(['rev-parse', '--verify', 'HEAD'], workdir, check=False)
    return head.returncode == 0


def checkpoint_ref_name(session_id: str, job_id: str, phase: str = 'before') -> str:
    if not SAFE_ID_RE.match(session_id) or not SAFE_ID_RE.match(job_id):
        raise ValueError('Invalid session or job identifier for checkpoint')
    suffix = '-after' if phase == 'after' else ''
    return f'refs/rove/checkpoints/{session_id}/{job_id}{suffix}'


def capture_checkpoint(workdir: str, session_id: str, job_id: str, phase: str = 'before') -> str:
    """Snapshot working tree (tracked + untracked) into a hidden Git ref without altering HEAD or index."""
    if not _is_git_repo_with_head(workdir):
        return ''
    ref = checkpoint_ref_name(session_id, job_id, phase=phase)
    tmp_fd, tmp_index = tempfile.mkstemp(prefix='rove-idx-')
    os.close(tmp_fd)
    # Remove the empty file so Git initializes a fresh temporary index cleanly.
    with contextlib.suppress(OSError):
        os.unlink(tmp_index)
    try:
        env = {'GIT_INDEX_FILE': tmp_index}
        _git(['read-tree', 'HEAD'], workdir, env=env)
        _git(['add', '-A'], workdir, env=env)
        tree_sha = _git(['write-tree'], workdir, env=env).stdout.strip()
        commit_sha = _git(
            ['commit-tree', tree_sha, '-p', 'HEAD', '-m', f'Rove checkpoint {session_id}/{job_id}'],
            workdir,
            env={
                'GIT_AUTHOR_NAME': 'Rove Checkpoint',
                'GIT_AUTHOR_EMAIL': 'rove@local',
                'GIT_COMMITTER_NAME': 'Rove Checkpoint',
                'GIT_COMMITTER_EMAIL': 'rove@local',
            },
        ).stdout.strip()
        _git(['update-ref', ref, commit_sha], workdir)
        return ref
    except Exception:
        return ''
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_index)


def diff_checkpoint(workdir: str, checkpoint_ref: str, checkpoint_after_ref: str = '') -> Dict[str, Any]:
    """Compute a turn-scoped diff between before/after checkpoint refs (or working tree while running)."""
    if not checkpoint_ref or not _is_git_repo_with_head(workdir):
        return {'available': False, 'scoped': False, 'diff': '', 'files': [], 'summary': ''}
    verify = _git(['rev-parse', '--verify', checkpoint_ref], workdir, check=False)
    if verify.returncode != 0:
        return {'available': False, 'scoped': False, 'diff': '', 'files': [], 'summary': ''}

    after_ref = (checkpoint_after_ref or '').strip()
    after_ok = bool(after_ref and _git(['rev-parse', '--verify', after_ref], workdir, check=False).returncode == 0)
    if after_ok:
        tracked_diff = _git(['diff', checkpoint_ref, after_ref, '--'], workdir, check=False).stdout
        name_status = _git(['diff', '--name-status', checkpoint_ref, after_ref, '--'], workdir, check=False).stdout
        files: List[Dict[str, str]] = []
        for line in name_status.splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                files.append({'status': parts[0].strip(), 'path': parts[1].strip()})
        return {
            'available': True,
            'scoped': True,
            'checkpoint_ref': checkpoint_ref,
            'checkpoint_after_ref': after_ref,
            'files': files,
            'diff': tracked_diff[:200000],
            'summary': f'{len(files)} 个文件发生变更（单轮固定快照）',
        }

    tracked_diff = _git(['diff', checkpoint_ref, '--'], workdir, check=False).stdout
    name_status = _git(['diff', '--name-status', checkpoint_ref, '--'], workdir, check=False).stdout
    files = []
    for line in name_status.splitlines():
        parts = line.split('\t', 1)
        if len(parts) == 2:
            files.append({'status': parts[0].strip(), 'path': parts[1].strip()})

    # Also include newly created untracked files not present in checkpoint_ref
    untracked_raw = _git(['ls-files', '--others', '--exclude-standard'], workdir, check=False).stdout
    untracked = [p.strip() for p in untracked_raw.splitlines() if p.strip()]
    extra_diffs = []
    for rel in untracked[:30]:
        files.append({'status': 'A', 'path': rel})
        # Use git diff --no-index /dev/null <file> to render clean unified diff for new files
        res = _git(['diff', '--no-index', '--', '/dev/null', rel], workdir, check=False)
        if res.stdout:
            extra_diffs.append(res.stdout)

    full_diff = tracked_diff
    if extra_diffs:
        full_diff = (full_diff.rstrip('\n') + '\n' + '\n'.join(extra_diffs)).lstrip('\n')
    return {
        'available': True,
        'scoped': False,
        'checkpoint_ref': checkpoint_ref,
        'files': files,
        'diff': full_diff[:200000],
        'summary': f'{len(files)} 个文件发生变更（实时工作区）',
    }


def _check_revert_conflicts(workdir: str, session_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    started_at = float(job.get('started') or 0)
    later = db.query(
        'SELECT id FROM jobs WHERE session_id=? AND id!=? AND COALESCE(reverted, 0)=0 AND COALESCE(started, 0) > ?',
        (session_id, job['id'], started_at),
    )
    active_rows = db.query(
        "SELECT j.id AS job_id, j.session_id, s.title, s.project_id, s.use_worktree, s.worktree_path "
        "FROM jobs j JOIN sessions s ON s.id=j.session_id "
        "WHERE j.session_id!=? AND j.status IN ('queued','running','waiting_approval','waiting_question')",
        (session_id,),
    )
    norm_workdir = str(Path(workdir).resolve())
    other_active: List[Dict[str, str]] = []
    for row in active_rows:
        sess_proj = db.one('SELECT * FROM projects WHERE id=?', (row.get('project_id'),))
        if not sess_proj:
            continue
        other_wd = str(Path(db.session_workdir(row, sess_proj)).resolve())
        if other_wd == norm_workdir:
            other_active.append({'session_id': row['session_id'], 'title': row.get('title') or row['session_id']})
    return {'later_jobs_count': len(later), 'other_active_sessions': other_active}


def revert_to_checkpoint(workdir: str, session_id: str, job_id: str, force: bool = False) -> Dict[str, Any]:
    """Restore workspace files to the pre-turn checkpoint and trim reverted turn state in SQLite."""
    job = db.one('SELECT * FROM jobs WHERE id=? AND session_id=?', (job_id, session_id))
    if not job:
        raise ValueError('未找到目标任务记录')
    checkpoint_ref = (job.get('checkpoint_ref') or '').strip()
    if not checkpoint_ref:
        raise RuntimeError('该回合未记录代码检查点（可能非 Git 仓库）')
    if not _is_git_repo_with_head(workdir):
        raise RuntimeError('当前工作区不是有效的 Git 仓库，无法执行检查点回滚')
    verify = _git(['rev-parse', '--verify', checkpoint_ref], workdir, check=False)
    if verify.returncode != 0:
        raise RuntimeError('检查点 Git 引用已不存在，无法回滚')

    if not force:
        conflicts = _check_revert_conflicts(workdir, session_id, job)
        if conflicts['later_jobs_count'] > 0 or conflicts['other_active_sessions']:
            reasons = []
            if conflicts['later_jobs_count'] > 0:
                reasons.append(f"该回合之后还有 {conflicts['later_jobs_count']} 轮未回滚修改")
            if conflicts['other_active_sessions']:
                titles = '、'.join(s['title'] for s in conflicts['other_active_sessions'][:3])
                reasons.append(f"其他会话（{titles}）正在同一工作区执行任务")
            return {
                'ok': False,
                'requires_confirm': True,
                'later_jobs_count': conflicts['later_jobs_count'],
                'other_active_sessions': conflicts['other_active_sessions'],
                'message': '检测到回滚冲突：' + '；'.join(reasons) + '。确认后将覆盖后续改动。',
            }

    # Restore working tree from checkpoint_ref via temporary index, keeping HEAD untouched.
    tmp_fd, tmp_index = tempfile.mkstemp(prefix='rove-rev-')
    os.close(tmp_fd)
    with contextlib.suppress(OSError):
        os.unlink(tmp_index)
    try:
        env = {'GIT_INDEX_FILE': tmp_index}
        _git(['read-tree', checkpoint_ref], workdir, env=env)
        _git(['checkout-index', '-a', '-f'], workdir, env=env)

        # Remove files that exist in current working tree but did not exist in checkpoint_ref
        ckpt_files_raw = _git(['ls-tree', '-r', '--name-only', checkpoint_ref], workdir).stdout
        ckpt_files = {line.strip() for line in ckpt_files_raw.splitlines() if line.strip()}

        curr_tracked_raw = _git(['ls-files'], workdir, check=False).stdout
        curr_untracked_raw = _git(['ls-files', '--others', '--exclude-standard'], workdir, check=False).stdout
        all_curr = {line.strip() for line in (curr_tracked_raw + '\n' + curr_untracked_raw).splitlines() if line.strip()}

        removed_count = 0
        base = Path(workdir).resolve()
        for rel in sorted(all_curr - ckpt_files, reverse=True):
            if rel.startswith('.worktrees/') or rel == '.worktrees':
                continue
            target = (base / rel).resolve()
            if target.is_relative_to(base) and target.is_file():
                target.unlink(missing_ok=True)
                removed_count += 1
        # Keep index aligned with HEAD
        _git(['read-tree', 'HEAD'], workdir, check=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_index)

    # Co-ordinate SQLite conversation history: mark this job and later jobs reverted & clear their events.
    started_at = float(job.get('started') or 0)
    with db.transaction() as conn:
        later_jobs = [
            r['id']
            for r in conn.execute(
                'SELECT id FROM jobs WHERE session_id=? AND (id=? OR COALESCE(started, 0) >= ?)',
                (session_id, job_id, started_at),
            )
        ]
        if later_jobs:
            placeholders = ','.join('?' for _ in later_jobs)
            conn.execute(f'UPDATE jobs SET reverted=1 WHERE id IN ({placeholders})', tuple(later_jobs))
            conn.execute(f'DELETE FROM events WHERE session_id=? AND job_id IN ({placeholders})', (session_id, *later_jobs))
        # Reset native thread pointers so the next turn re-synthesizes cleanly from remaining DB history
        conn.execute(
            'UPDATE sessions SET native_id=NULL, native_codex_id=NULL, native_antigravity_id=NULL, updated=? WHERE id=?',
            (db.time.time(), session_id),
        )
    db.event(session_id, None, 'notice', {
        'text': f'⏪ 已将代码工作区与对话历史同步回滚至该回合执行前状态（清理新增文件 {removed_count} 个）。'
    })
    return {'ok': True, 'reverted_job_id': job_id, 'removed_files': removed_count}
