"""Cross-engine context bridging and handoff memory synthesis.

Enables seamless switching between different AI engines (Codex and Antigravity)
within the same conversation session without losing conversational memory,
architectural decisions, or awareness of files modified.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

from . import memories
from . import store as db

BRIDGE_START_TAG = '<!-- WORKSTATION_BRIDGE_START -->'
BRIDGE_END_TAG = '<!-- WORKSTATION_BRIDGE_END -->'


def strip_injected_context(text: str) -> str:
    """Strip background-injected handoff, memory, or design context blocks from a user prompt."""
    if not text:
        return ''
    cleaned = text
    # 1. Strip explicit bridge tags
    cleaned = re.sub(
        r'<!-- WORKSTATION_BRIDGE_START -->.*?<!-- WORKSTATION_BRIDGE_END -->\s*',
        '',
        cleaned,
        flags=re.DOTALL,
    )
    # 2. Strip legacy untagged bridge block
    cleaned = re.sub(
        r'# \[跨引擎接力上下文与历史记忆.*?直接精准响应用户本次的新指令[^\n]*\n*',
        '',
        cleaned,
        flags=re.DOTALL,
    )
    # 3. Strip injected project memory header if synced back from native transcript
    cleaned = re.sub(
        r'\[项目持久记忆与全局架构约定[^\]]*\].*?请严格遵从以上项目记忆与红线约定。\s*',
        '',
        cleaned,
        flags=re.DOTALL,
    )
    # 4. Strip read-only mode prefix if synced back
    cleaned = re.sub(
        r'^\[只读规划模式：[^\]]+\]\s*',
        '',
        cleaned.strip(),
    )
    return cleaned.strip()


def extract_session_turns(
    session_id: str,
    exclude_job_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract ordered turns from a session, capturing prompts, replies, engines, and file changes."""
    events = db.query(
        'SELECT id, job_id, kind, payload, created FROM events WHERE session_id=? ORDER BY id ASC',
        (session_id,),
    )
    if not events:
        return []

    turns_map: Dict[str, Dict[str, Any]] = OrderedDict()
    current_sync_key: Optional[str] = None

    for e in events:
        jid = e.get('job_id')
        if exclude_job_id and jid == exclude_job_id:
            continue

        kind = e.get('kind')
        try:
            payload = json.loads(e.get('payload') or '{}')
        except Exception:
            continue

        if jid:
            turn_key = jid
        else:
            if kind == 'user' or current_sync_key is None:
                current_sync_key = f"sync_{e.get('id', len(turns_map))}"
            turn_key = current_sync_key

        if turn_key not in turns_map:
            turns_map[turn_key] = {
                'job_id': turn_key,
                'prompt': '',
                'engine': None,
                'reply': '',
                'files': set(),
                'commands': [],
                'created': e.get('created', 0),
            }
        turn = turns_map[turn_key]

        if kind == 'user':
            turn['prompt'] = strip_injected_context((payload.get('text') or '').strip())
            if payload.get('engine'):
                turn['engine'] = payload['engine']
            elif payload.get('source') == 'antigravity_sync':
                turn['engine'] = 'antigravity'
            elif payload.get('source') == 'codex_sync':
                turn['engine'] = 'codex'
        elif kind == 'antigravity':
            turn['engine'] = payload.get('engine') or 'antigravity'
            evt = payload.get('event')
            if evt == 'agy_step':
                step_kind = payload.get('step_kind')
                if step_kind == 'agent' and payload.get('text'):
                    turn['reply'] = payload['text'].strip()
                elif step_kind == 'change':
                    for c in (payload.get('changes') or []):
                        if c.get('path'):
                            turn['files'].add(c['path'])
                elif step_kind == 'command' and payload.get('command'):
                    turn['commands'].append(payload['command'])
            elif evt == 'result':
                res = payload.get('result') or payload
                if isinstance(res, dict):
                    rtext = res.get('text') or res.get('response')
                    if rtext:
                        turn['reply'] = rtext.strip()
        elif kind == 'codex':
            turn['engine'] = 'codex'
            method = payload.get('method')
            params = payload.get('params') or {}
            item = params.get('item') or {}
            if method == 'item/completed' and item.get('type') == 'agentMessage' and item.get('text'):
                turn['reply'] = item['text'].strip()
            elif method == 'item/agentMessage/delta' and params.get('delta'):
                turn['reply'] = (turn['reply'] + params['delta']).strip()
            elif item.get('type') == 'fileChange':
                for c in (item.get('changes') or []):
                    if c.get('path'):
                        turn['files'].add(c['path'])
            elif item.get('type') == 'commandExecution' and item.get('command'):
                turn['commands'].append(item['command'])

    return list(turns_map.values())


def _substantive_turns(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter turns to those that actually completed with a reply, file edit, or command execution."""
    completed = [t for t in turns if t.get('reply') or t.get('files') or t.get('commands')]
    return completed if completed else turns


def should_bridge_context(
    session: Dict[str, Any],
    target_engine: str,
    exclude_job_id: Optional[str] = None,
) -> bool:
    """Determine whether a cross-engine handoff context injection is needed."""
    session_id = session.get('id')
    if not session_id:
        return False

    raw_turns = extract_session_turns(session_id, exclude_job_id=exclude_job_id)
    if not raw_turns:
        return False

    turns = _substantive_turns(raw_turns)

    # Check the engine of the last completed turn with content
    prev_turns_with_engine = [t for t in turns if t.get('engine')]
    if not prev_turns_with_engine:
        # Fall back to session['last_engine'] or session['engine']
        last_engine = session.get('last_engine') or session.get('engine')
        return bool(last_engine and last_engine != target_engine)

    last_engine = prev_turns_with_engine[-1]['engine']
    if last_engine != target_engine:
        return True

    # If target engine has never run before in this session, but another engine did
    from . import agents
    if not agents.get_session_native_agent_id(session, target_engine):
        return any(t.get('engine') and t.get('engine') != target_engine for t in turns)

    return False


def _compress_reply_for_handoff(reply: str, max_chars: int = 900) -> str:
    """Distill agent reply by collapsing code blocks and excessive whitespace to save tokens."""
    if not reply:
        return ''
    text = reply.strip()
    # Replace multi-line fenced code blocks (>3 lines) with a concise placeholder
    def _collapse_code(match: re.Match) -> str:
        block = match.group(0)
        if block.count('\n') > 3:
            return '[代码详见对应本地文件]'
        return block

    text = re.sub(r'```[\s\S]*?```', _collapse_code, text)
    # Remove horizontal rules and collapse multiple blank lines
    text = re.sub(r'(?m)^\s*[-*_]{3,}\s*$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + '\n…（前序长文已精简）'
    return text


def _normalize_rel_path(fpath: str, proj_path: str) -> str:
    if not fpath:
        return ''
    clean = fpath.strip()
    if proj_path:
        prefix = proj_path.rstrip('/') + '/'
        if clean.startswith(prefix):
            return clean[len(prefix):]
    return clean


def build_handoff_context(
    session: Dict[str, Any],
    project: Dict[str, Any],
    target_engine: str,
    max_history_turns: int = 4,
    max_reply_chars: int = 900,
    exclude_job_id: Optional[str] = None,
) -> str:
    """Synthesize a token-efficient Markdown handoff context block when switching engines."""
    session_id = session.get('id')
    if not session_id:
        return ''

    raw_turns = extract_session_turns(session_id, exclude_job_id=exclude_job_id)
    if not raw_turns:
        return ''

    turns = _substantive_turns(raw_turns)
    if not turns:
        return ''

    # Check if handoff is needed
    prev_turns_with_engine = [t for t in turns if t.get('engine')]
    if not prev_turns_with_engine:
        last_engine = session.get('last_engine') or session.get('engine')
        needed = bool(last_engine and last_engine != target_engine)
    else:
        last_engine = prev_turns_with_engine[-1]['engine']
        if last_engine != target_engine:
            needed = True
        else:
            from . import agents
            if not agents.get_session_native_agent_id(session, target_engine):
                needed = any(t.get('engine') and t.get('engine') != target_engine for t in turns)
            else:
                needed = False

    if not needed:
        return ''

    # Identify the previous engine
    prev_engines = [t['engine'] for t in turns if t.get('engine')]
    from_engine = prev_engines[-1] if prev_engines else (session.get('last_engine') or 'previous')

    # Check if target engine already has an active native session in this conversation
    from . import agents
    has_native_target = bool(agents.get_session_native_agent_id(session, target_engine))

    # Determine if we can do an Incremental Delta Handoff (only turns since target_engine last ran)
    last_target_idx = -1
    if has_native_target:
        for i in range(len(turns) - 1, -1, -1):
            if turns[i].get('engine') == target_engine:
                last_target_idx = i
                break

    is_delta_handoff = has_native_target and last_target_idx >= 0 and last_target_idx < len(turns) - 1
    scope_turns = turns[last_target_idx + 1:] if is_delta_handoff else turns

    # Collect modified files (only delta files if resuming existing native session, normalized to relative paths)
    proj_path = str(project.get('path') or '')
    all_files: Set[str] = set()
    for t in scope_turns:
        for f in t['files']:
            norm = _normalize_rel_path(f, proj_path)
            if norm:
                all_files.add(norm)

    # Focus on the most recent turns, plus the first turn as an anchor if starting fresh after many turns
    valid_scope = [t for t in scope_turns if t.get('prompt') or t.get('reply')]
    recent_turns = valid_scope[-max_history_turns:]
    anchor_turn = None
    if not is_delta_handoff and len(valid_scope) > max_history_turns:
        anchor_turn = valid_scope[0]

    mode_label = '增量差量交接' if is_delta_handoff else '跨引擎接力'
    lines: List[str] = [
        BRIDGE_START_TAG,
        f'# [跨引擎接力上下文与历史记忆（{mode_label}，系统自动合成）]',
        f'- **执行交接**：`{from_engine.upper()}` ➔ `{target_engine.upper()}` | **项目**：`{project.get("name", "当前项目")}`',
        '',
        '### 1. 历史任务与核心提问',
    ]

    if anchor_turn and anchor_turn.get('prompt'):
        a_text = anchor_turn['prompt'].replace('\n', ' ').strip()
        if len(a_text) > 140:
            a_text = a_text[:140] + '…'
        lines.append(f"- [首轮总目标]: {a_text}")

    for idx, t in enumerate(recent_turns, 1):
        p_text = t.get('prompt', '').replace('\n', ' ').strip()
        if len(p_text) > 140:
            p_text = p_text[:140] + '…'
        lines.append(f"- 轮次 #{idx}: {p_text}")

    # Add the latest substantive reply from the previous engine (distilled)
    last_reply_turn = next((t for t in reversed(recent_turns) if t.get('reply')), None)
    if last_reply_turn and last_reply_turn.get('reply'):
        clean_reply = _compress_reply_for_handoff(last_reply_turn['reply'], max_chars=max_reply_chars)
        lines.extend([
            '',
            f"### 2. 前序模型（{from_engine.upper()}）已达成方案与最终结论",
            clean_reply,
        ])

    # Add file modification records (capped at 12 files for token efficiency)
    if all_files:
        lines.extend([
            '',
            f"### 3. 前序已发生的代码与文件修改（共 {len(all_files)} 个文件）",
        ])
        for fpath in sorted(all_files)[:12]:
            lines.append(f"- `{fpath}`")
        if len(all_files) > 12:
            lines.append(f"- … 另有 {len(all_files) - 12} 个文件")

    # Only inject project persistent memories if target engine has not initialized its native session yet
    if not is_delta_handoff:
        try:
            mem_ctx = memories.build_memory_context(project['id'])
            if mem_ctx:
                lines.extend([
                    '',
                    '### 4. 项目全局架构约定与硬性红线',
                    mem_ctx,
                ])
        except Exception:
            pass

    # Instructions for seamless continuation
    lines.extend([
        '',
        '### 5. 接力执行要求（静默无感模式）',
        f"用户已在当前会话中无缝切换至你（{target_engine.upper()}）。"
        "此接力背景由系统在后台静默注入，对用户界面完全隐藏。"
        "请直接基于上述已有成果、文件修改与对话共识自然地响应用户下方的新指令；"
        "切勿在回复中提及“跨引擎接力上下文”、“系统注入背景”或“交接文档”等字样，保持宛如同一模型连续对话的自然体验。",
        BRIDGE_END_TAG,
    ])

    return '\n'.join(lines)


_OVERFLOW_PATTERNS = (
    re.compile(r'context[_\s-]*(?:length|window)[_\s-]*(?:exceeded|full|limit)', re.IGNORECASE),
    re.compile(r'maximum\s+context\s+length', re.IGNORECASE),
    re.compile(r'prompt\s+is\s+too\s+long', re.IGNORECASE),
    re.compile(r'too\s+many\s+tokens', re.IGNORECASE),
    re.compile(r'compact(?:ion)?\s+failed|invalid_encrypted_content', re.IGNORECASE),
)


def is_context_overflow_error(error_text: str) -> bool:
    """Return True if an upstream error indicates context window exhaustion or compact failure."""
    if not error_text:
        return False
    return any(pat.search(error_text) for pat in _OVERFLOW_PATTERNS)


def has_turn_side_effects(session_id: str, job_id: str) -> bool:
    """Fail-closed check: return True if a job produced assistant output, file changes, or commands."""
    rows = db.query(
        'SELECT kind, payload FROM events WHERE session_id=? AND job_id=?',
        (session_id, job_id),
    )
    for row in rows:
        kind = row.get('kind')
        if kind in ('user', 'notice', 'error'):
            continue
        try:
            payload = json.loads(row.get('payload') or '{}')
        except Exception:
            return True
        if kind == 'codex':
            method = payload.get('method') or ''
            item = (payload.get('params') or {}).get('item') or {}
            if method in ('item/agentMessage/delta', 'item/completed') and item.get('type') != 'userMessage':
                return True
            if item.get('type') in ('fileChange', 'commandExecution', 'mcpToolCall'):
                return True
        elif kind == 'antigravity':
            if payload.get('event') == 'agy_step' and payload.get('step_kind') in ('agent', 'change', 'command'):
                return True
            if payload.get('event') == 'result':
                return True
        else:
            return True
    return False


def build_rollover_context(
    session: Dict[str, Any],
    project: Dict[str, Any],
    engine: str,
    max_history_turns: int = 4,
    max_reply_chars: int = 900,
    exclude_job_id: Optional[str] = None,
) -> str:
    """Synthesize a same-engine rollover handoff block when native context window overflows."""
    session_id = session.get('id')
    if not session_id:
        return ''
    raw_turns = extract_session_turns(session_id, exclude_job_id=exclude_job_id)
    turns = _substantive_turns(raw_turns)
    if not turns:
        return ''

    proj_path = str(project.get('path') or '')
    all_files: Set[str] = set()
    for t in turns:
        for f in t['files']:
            norm = _normalize_rel_path(f, proj_path)
            if norm:
                all_files.add(norm)

    valid = [t for t in turns if t.get('prompt') or t.get('reply')]
    recent = valid[-max_history_turns:]
    anchor = valid[0] if len(valid) > max_history_turns else None

    lines: List[str] = [
        BRIDGE_START_TAG,
        f'# [上下文满窗自动换窗交接 ({engine.upper()})]',
        f'- **项目**: `{project.get("name", "当前项目")}`',
        '',
        '### 1. 任务目标与近期轮次',
    ]
    if anchor and anchor.get('prompt'):
        lines.append(f"- [首轮总目标]: {anchor['prompt'].replace(chr(10), ' ').strip()[:160]}")
    for idx, t in enumerate(recent, 1):
        lines.append(f"- 轮次 #{idx}: {t.get('prompt', '').replace(chr(10), ' ').strip()[:140]}")

    last_reply_turn = next((t for t in reversed(recent) if t.get('reply')), None)
    if last_reply_turn and last_reply_turn.get('reply'):
        lines.extend([
            '',
            '### 2. 上一窗口最新进展',
            _compress_reply_for_handoff(last_reply_turn['reply'], max_chars=max_reply_chars),
        ])

    if all_files:
        lines.extend(['', f'### 3. 已修改文件清单（共 {len(all_files)} 个）'])
        for fpath in sorted(all_files)[:12]:
            lines.append(f'- `{fpath}`')

    lines.extend([
        '',
        '### 4. 续接要求',
        '原原生上下文窗口已满，系统已为你开启全新窗口并静默注入上述核心状态。请直接读取本地文件继续响应用户指令。',
        BRIDGE_END_TAG,
    ])
    return '\n'.join(lines)


def inspect_handoff_context(
    session: Dict[str, Any],
    project: Dict[str, Any],
    target_engine: str,
    target_model: Optional[str] = None,
    max_history_turns: int = 4,
) -> Dict[str, Any]:
    """Return a structured preview of cross-engine handoff context and any omitted historical turns."""
    session_id = session.get('id')
    if not session_id:
        return {'needed': False, 'total_turns': 0, 'included_turns': 0, 'omitted_turns': [], 'preview_text': ''}

    raw_turns = extract_session_turns(session_id)
    turns = _substantive_turns(raw_turns)
    total = len(turns)
    included = turns[-max_history_turns:] if total > max_history_turns else turns
    omitted = turns[:-max_history_turns] if total > max_history_turns else []

    preview_text = build_handoff_context(session, project, target_engine, max_history_turns=max_history_turns)
    if not preview_text and turns:
        preview_text = build_rollover_context(session, project, max_history_turns=max_history_turns)

    proj_path = (project or {}).get('path') or ''
    changed_files: set[str] = set()
    for t in turns:
        for f in (t.get('files') or []):
            changed_files.add(_normalize_rel_path(f, proj_path))

    omitted_items = [
        {
            'job_id': t.get('job_id') or '',
            'engine': t.get('engine') or '',
            'prompt': (t.get('prompt') or '')[:300],
            'reply_preview': (t.get('reply') or '')[:300],
        }
        for t in omitted
    ]
    chars = len(preview_text)
    return {
        'needed': bool(preview_text),
        'from_engine': session.get('last_engine') or session.get('engine') or '',
        'target_engine': target_engine,
        'target_model': target_model or '',
        'total_turns': total,
        'included_turns': len(included),
        'omitted_count': len(omitted_items),
        'omitted_turns': omitted_items,
        'changed_files': sorted(changed_files)[:20],
        'context_chars': chars,
        'estimated_tokens': max(1, chars // 4) if chars else 0,
        'preview_text': preview_text,
    }
