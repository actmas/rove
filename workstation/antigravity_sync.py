"""Two-way project, session metadata, real-time status and transcript sync with local Antigravity."""
import contextlib
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any
import urllib.parse
import weakref

from . import store as db
from . import context_bridge

logger = logging.getLogger(__name__)

_meta_lock = threading.Lock()
_session_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_session_locks_guard = threading.Lock()
_sync_cache: dict[tuple[str, str], tuple[float, tuple, dict]] = {}


def _allowed_roots() -> list[Path]:
    try:
        cfg = db.config()
    except OSError:
        cfg = {}
    roots = []
    for raw in cfg.get('allowed_roots', []):
        if not raw:
            continue
        with contextlib.suppress(OSError):
            roots.append(Path(raw).expanduser().resolve())
    return roots


def _is_under_allowed_roots(path: Path, roots: list[Path]) -> bool:
    if not roots:
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def _get_session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def antigravity_home() -> Path | None:
    try:
        cfg = db.config()
    except OSError:
        cfg = {}
    configured = cfg.get('antigravity_home') or os.environ.get('WORKSTATION_ANTIGRAVITY_HOME')
    if configured:
        p = Path(configured).expanduser().resolve()
        if (p / 'conversation_summaries.db').is_file():
            return p
    default_home = Path.home() / '.gemini' / 'antigravity'
    if (default_home / 'conversation_summaries.db').is_file():
        return default_home.resolve()
    return None


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = f'file:{urllib.parse.quote(str(path))}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_uri_to_path(raw_uri: str | None) -> str:
    if not raw_uri:
        return ''
    raw = str(raw_uri).strip()
    if raw.startswith('file://'):
        unquoted = urllib.parse.unquote(raw.removeprefix('file://'))
        return unquoted
    return urllib.parse.unquote(raw)


def extract_user_request(text: str) -> str:
    """Extract prompt text inside <USER_REQUEST> tags or fallback to cleaned text."""
    if not text:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", text, re.DOTALL)
    if m:
        extracted = m.group(1).strip()
    else:
        # Strip known system wrappers if any
        cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, flags=re.DOTALL)
        extracted = cleaned.strip()
    return context_bridge.strip_injected_context(extracted)


def _parse_time_to_ts(val: Any) -> float:
    if not val:
        return time.time()
    if isinstance(val, (int, float)):
        return float(val)
    raw = str(val).strip()
    try:
        clean = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean)
        return dt.timestamp()
    except Exception:
        pass
    try:
        dt = datetime.strptime(raw[:19], '%Y-%m-%d %H:%M:%S')
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return time.time()


def sync_antigravity_projects_and_sessions(force: bool = False, restore_dismissed: bool = False) -> dict:
    home = antigravity_home()
    if not home:
        return {'status': 'unavailable', 'projects_count': 0, 'sessions_count': 0}

    summaries_db = home / 'conversation_summaries.db'
    if not summaries_db.is_file():
        return {'status': 'unavailable', 'projects_count': 0, 'sessions_count': 0}

    try:
        st = summaries_db.stat()
        sig = (st.st_mtime, st.st_size)
    except OSError:
        sig = (0, 0)

    cache_key = (str(db.DATA), str(home))
    now_mono = time.monotonic()
    if not force and not restore_dismissed:
        cached = _sync_cache.get(cache_key)
        if cached and (now_mono - cached[0] < 1.5 or cached[1] == sig):
            return cached[2]

    with _meta_lock:
        if restore_dismissed:
            with db.transaction() as conn:
                conn.execute('DELETE FROM dismissed_antigravity_threads')

        with db.transaction() as conn:
            dismissed = {
                row['native_id'] for row in conn.execute('SELECT native_id FROM dismissed_antigravity_threads')
            }
            existing_projects = {
                row['path']: row['id'] for row in conn.execute('SELECT id, path FROM projects')
            }
            existing_sessions = {}
            for row in conn.execute(
                "SELECT id, project_id, title, engine, native_id, native_antigravity_id, updated, pinned "
                "FROM sessions WHERE native_antigravity_id IS NOT NULL OR (engine='antigravity' AND native_id IS NOT NULL)"
            ):
                rdict = dict(row)
                aid = rdict.get('native_antigravity_id') or rdict.get('native_id')
                if aid:
                    existing_sessions[aid] = rdict

        rows = []
        with contextlib.suppress(sqlite3.Error, OSError):
            with contextlib.closing(_ro_connect(summaries_db)) as c_conn:
                for r in c_conn.execute(
                    'SELECT conversation_id, title, preview, step_count, last_modified_time, last_user_input_time, workspace_uris, not_fully_idle FROM conversation_summaries'
                ):
                    rows.append(dict(r))

        synced_count = 0
        now = time.time()
        allowed = _allowed_roots()
        fallback_proj_path = None
        for ep in existing_projects:
            with contextlib.suppress(OSError):
                if Path(ep).is_dir() and _is_under_allowed_roots(Path(ep), allowed):
                    fallback_proj_path = ep
                    break
        if not fallback_proj_path:
            cwd_resolved = Path.cwd().resolve()
            if _is_under_allowed_roots(cwd_resolved, allowed):
                fallback_proj_path = str(cwd_resolved)
            elif allowed and allowed[0].is_dir():
                fallback_proj_path = str(allowed[0])

        new_projects: list[tuple[str, str, str]] = []
        new_sessions: list[tuple] = []
        updated_sessions: list[tuple] = []

        for r in rows:
            cid = r.get('conversation_id')
            if not cid or (cid in dismissed and not restore_dismissed):
                continue

            # Determine project path from workspace_uris
            target_proj_path = None
            uris_raw = r.get('workspace_uris') or ''
            try:
                uris = json.loads(uris_raw) if isinstance(uris_raw, str) and uris_raw.startswith('[') else [uris_raw]
            except Exception:
                uris = [uris_raw]
            for u in uris:
                clean = _clean_uri_to_path(u)
                if clean and Path(clean).is_dir():
                    resolved_cand = Path(clean).resolve()
                    parts = set(resolved_cand.parts)
                    if (
                        'runtime' in parts
                        or 'smoke-project' in parts
                        or any(p.startswith('.') or p.startswith('workstation-') for p in resolved_cand.parts)
                    ):
                        continue
                    if not _is_under_allowed_roots(resolved_cand, allowed):
                        continue
                    target_proj_path = str(resolved_cand)
                    break

            if not target_proj_path:
                sess_existing = existing_sessions.get(cid)
                if sess_existing:
                    # Keep existing session's project if already registered
                    for p_path, p_id in existing_projects.items():
                        if p_id == sess_existing['project_id']:
                            target_proj_path = p_path
                            break
                if not target_proj_path:
                    target_proj_path = fallback_proj_path
            if not target_proj_path:
                continue

            # Ensure project exists in db
            pid = existing_projects.get(target_proj_path)
            if not pid:
                pid = secrets.token_hex(8)
                pname = Path(target_proj_path).name or '项目'
                new_projects.append((pid, pname, target_proj_path))
                existing_projects[target_proj_path] = pid

            created_ts = _parse_time_to_ts(r.get('last_user_input_time') or r.get('last_modified_time'))
            updated_ts = _parse_time_to_ts(r.get('last_modified_time'))
            raw_title = (r.get('title') or '').strip()
            raw_prev = (r.get('preview') or '').strip()
            title = raw_title or raw_prev[:40] or 'Antigravity 会话'
            not_idle = int(bool(r.get('not_fully_idle')))

            sess = existing_sessions.get(cid)
            if not sess:
                sess_id = f"agy_{secrets.token_hex(8)}"
                new_sessions.append((sess_id, pid, title, cid, cid, created_ts, updated_ts, not_idle))
                existing_sessions[cid] = {
                    'id': sess_id, 'project_id': pid, 'title': title, 'native_id': cid,
                    'native_antigravity_id': cid,
                    'updated': updated_ts, 'pinned': 0,
                }
                synced_count += 1
            else:
                updated_sessions.append((title, updated_ts, not_idle, cid, sess['id']))
                synced_count += 1

        if new_projects or new_sessions or updated_sessions:
            with db.transaction() as conn:
                if new_projects:
                    conn.executemany(
                        'INSERT OR IGNORE INTO projects(id, name, path) VALUES(?, ?, ?)',
                        new_projects,
                    )
                if new_sessions:
                    conn.executemany(
                        """
                        INSERT INTO sessions(id, project_id, engine, last_engine, title, native_id, native_antigravity_id, created, updated,
                                            model, effort, mode, fast, pinned, antigravity_not_idle)
                        VALUES(?, ?, 'antigravity', 'antigravity', ?, ?, ?, ?, ?, 'gemini-4-argon-medium', 'high', 'auto', 0, 0, ?)
                        """,
                        new_sessions,
                    )
                if updated_sessions:
                    conn.executemany(
                        """
                        UPDATE sessions
                        SET title=COALESCE(?, title), updated=MAX(updated, ?), antigravity_not_idle=?,
                            native_antigravity_id=COALESCE(native_antigravity_id, ?)
                        WHERE id=?
                        """,
                        updated_sessions,
                    )

        res = {
            'status': 'ready',
            'home': str(home),
            'projects_count': len(existing_projects),
            'sessions_count': len(existing_sessions),
            'synced_count': synced_count,
        }
        _sync_cache[cache_key] = (now_mono, sig, res)
        return res


def external_activity_marker(conv_id: str, home: Path | None = None) -> str:
    """Return a snapshot marker for an active Antigravity conversation summary, or '' if idle."""
    if not conv_id:
        return ''
    home = home or antigravity_home()
    if not home:
        return ''
    summaries_db = home / 'conversation_summaries.db'
    if not summaries_db.is_file():
        return ''
    with contextlib.suppress(sqlite3.Error, OSError):
        with contextlib.closing(_ro_connect(summaries_db)) as conn:
            row = conn.execute(
                'SELECT step_count, last_modified_time FROM conversation_summaries WHERE conversation_id=? AND not_fully_idle = 1 LIMIT 1',
                (conv_id,),
            ).fetchone()
            if row:
                return f"{row['step_count']}:{row['last_modified_time']}"
    return ''


def mark_external_stopped(session_id: str, conv_id: str, home: Path | None = None) -> None:
    """Record that the user explicitly stopped an externally-running Antigravity task from Rove."""
    marker = external_activity_marker(conv_id, home) or f'stopped:{int(time.time())}'
    db.execute(
        'UPDATE sessions SET external_stopped_at=?, external_stopped_marker=?, antigravity_not_idle=0 WHERE id=?',
        (time.time(), marker, session_id),
    )


def live_antigravity_states(sessions_list: list[dict], active_local_session_ids: set[str]) -> dict[str, dict]:
    """Map session_id -> external_active for Antigravity sessions running on Mac."""
    home = antigravity_home()
    if not home:
        return {}

    summaries_db = home / 'conversation_summaries.db'
    if not summaries_db.is_file():
        return {}

    now = time.time()
    active_markers: dict[str, tuple[str, float]] = {}
    with contextlib.suppress(sqlite3.Error, OSError):
        with contextlib.closing(_ro_connect(summaries_db)) as conn:
            for r in conn.execute(
                'SELECT conversation_id, step_count, last_modified_time FROM conversation_summaries WHERE not_fully_idle = 1'
            ):
                cid = r['conversation_id']
                marker = f"{r['step_count']}:{r['last_modified_time']}"
                mod_ts = _parse_time_to_ts(r['last_modified_time'])
                t_path = home / 'brain' / cid / '.system_generated' / 'logs' / 'transcript.jsonl'
                with contextlib.suppress(OSError):
                    if t_path.is_file():
                        mod_ts = max(mod_ts, t_path.stat().st_mtime)
                active_markers[cid] = (marker, mod_ts)

    res = {}
    for s in sessions_list:
        nid = db.get_antigravity_native_id(s)
        if not nid:
            continue
        sid = s['id']
        info = active_markers.get(nid)
        if not info or sid in active_local_session_ids:
            res[sid] = {'external_active': False, 'writer_locked': False}
            continue
        marker, latest_ts = info
        stopped_marker = (s.get('external_stopped_marker') or '').strip()
        stopped_at = float(s.get('external_stopped_at') or 0.0)
        if stopped_marker == marker:
            is_active = False
        elif stopped_at > 0 and (latest_ts <= stopped_at + 20.0 or (now - stopped_at) <= 20.0):
            # Post-cancel cleanup steps written shortly after Stop must not re-lock the session
            is_active = False
            with contextlib.suppress(Exception):
                db.execute('UPDATE sessions SET external_stopped_marker=? WHERE id=?', (marker, sid))
        elif (now - latest_ts) > 180.0:
            # Stale not_fully_idle flag (e.g. background daemon or aborted turn with no activity for >3m)
            is_active = False
        else:
            is_active = True
        res[sid] = {'external_active': is_active, 'writer_locked': is_active}
    return res


def sync_antigravity_session_events(session_id: str) -> int:
    """Read transcript steps from ~/.gemini/antigravity/brain/<conv_id> and append missing events."""
    with _get_session_lock(session_id):
        session = db.one('SELECT * FROM sessions WHERE id = ?', (session_id,))
        conv_id = db.get_antigravity_native_id(session)
        if not session or not conv_id:
            return 0

        # Do not race against our own live Antigravity worker streaming real-time steps.
        active_job = db.one(
            "SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting')",
            (session_id,),
        )
        if active_job:
            return 0

        home = antigravity_home()
        if not home:
            return 0

        transcript_path = home / 'brain' / conv_id / '.system_generated' / 'logs' / 'transcript.jsonl'
        if not transcript_path.is_file():
            return 0

        synced_step = int(session.get('antigravity_synced_step') if session.get('antigravity_synced_step') is not None else -1)
        next_synced_step = synced_step
        saved_offset = int(session.get('antigravity_transcript_offset') or 0)
        try:
            file_size = transcript_path.stat().st_size
        except OSError:
            return 0
        if saved_offset > file_size or synced_step < 0:
            saved_offset = 0
        if saved_offset == file_size and synced_step >= 0:
            return 0

        # Gather existing user texts to avoid duplicate user prompts
        existing_user_texts: set[str] = set()
        for r in db.query("SELECT payload FROM events WHERE session_id=? AND kind='user'", (session_id,)):
            with contextlib.suppress(Exception):
                t = context_bridge.strip_injected_context((json.loads(r['payload']).get('text') or '').strip())
                if t:
                    existing_user_texts.add(t)
        known_step_ids = {
            r['step_id'] for r in db.query(
                "SELECT json_extract(payload, '$.step_id') AS step_id FROM events WHERE session_id=? AND kind='antigravity' AND step_id IS NOT NULL",
                (session_id,),
            )
        }
        existing_agent_texts = {
            r['text'] for r in db.query(
                "SELECT json_extract(payload, '$.text') AS text FROM events WHERE session_id=? AND kind='antigravity' AND json_extract(payload, '$.step_kind')='agent'",
                (session_id,),
            ) if r.get('text')
        }

        pending_events: list[tuple[str, dict, float]] = []
        new_offset = saved_offset

        try:
            with transcript_path.open('rb') as f:
                if saved_offset > 0:
                    f.seek(saved_offset)
                while True:
                    raw_bytes = f.readline()
                    if not raw_bytes:
                        break
                    if not raw_bytes.endswith(b'\n') and f.tell() == file_size:
                        # Incomplete trailing line being written; wait for next tick if not valid JSON
                        stripped_check = raw_bytes.decode('utf-8', errors='replace').strip()
                        try:
                            json.loads(stripped_check)
                        except ValueError:
                            break
                    new_offset = f.tell()
                    stripped = raw_bytes.decode('utf-8', errors='replace').strip()
                    if not stripped:
                        continue
                    try:
                        st = json.loads(stripped)
                    except ValueError:
                        continue

                    idx = st.get('step_index')
                    if idx is None or idx <= synced_step:
                        continue
                    if idx > next_synced_step:
                        next_synced_step = idx

                    st_type = st.get('type') or ''
                    created_raw = st.get('created_at')
                    created_ts = _parse_time_to_ts(created_raw)

                    if st_type == 'USER_INPUT':
                        raw_content = st.get('content') or ''
                        user_prompt = extract_user_request(raw_content)
                        if user_prompt:
                            # check if already in existing
                            if user_prompt not in existing_user_texts:
                                existing_user_texts.add(user_prompt)
                                pending_events.append(('user', {
                                    'text': user_prompt,
                                    'source': 'antigravity_sync',
                                    'item_id': f'step-{idx}',
                                }, created_ts))

                    elif st_type == 'PLANNER_RESPONSE':
                        thinking = (st.get('thinking') or '').strip()
                        if thinking:
                            sid_think = f'agy-think-{conv_id}-{idx}'
                            if sid_think not in known_step_ids:
                                known_step_ids.add(sid_think)
                                pending_events.append(('antigravity', {
                                    'event': 'agy_step',
                                    'step_kind': 'reasoning',
                                    'step_id': sid_think,
                                    'text': thinking,
                                    'completed': True,
                                }, created_ts))

                        content = (st.get('content') or '').strip()
                        if content:
                            sid_msg = f'agy-msg-{conv_id}-{idx}'
                            if sid_msg not in known_step_ids and content not in existing_agent_texts:
                                known_step_ids.add(sid_msg)
                                existing_agent_texts.add(content)
                                pending_events.append(('antigravity', {
                                    'event': 'agy_step',
                                    'step_kind': 'agent',
                                    'step_id': sid_msg,
                                    'text': content,
                                    'completed': True,
                                }, created_ts))

                        tool_calls = st.get('tool_calls') or []
                        for t_idx, tc in enumerate(tool_calls):
                            tname = tc.get('name') or tc.get('tool') or ''
                            args = tc.get('arguments') or tc.get('argumentsJson') or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}

                            if 'run_command' in tname or 'command' in tname:
                                cmd = args.get('CommandLine') or args.get('command') or 'shell'
                                sid_cmd = f'agy-cmd-{conv_id}-{idx}-{t_idx}'
                                if sid_cmd not in known_step_ids:
                                    known_step_ids.add(sid_cmd)
                                    pending_events.append(('antigravity', {
                                        'event': 'agy_step',
                                        'step_kind': 'command',
                                        'step_id': sid_cmd,
                                        'command': cmd,
                                        'completed': True,
                                    }, created_ts))

                            elif any(k in tname for k in ('write_file', 'edit_file', 'create_file')):
                                path = args.get('Path') or args.get('path') or ''
                                sid_chg = f'agy-chg-{conv_id}-{idx}-{t_idx}'
                                if sid_chg not in known_step_ids:
                                    known_step_ids.add(sid_chg)
                                    pending_events.append(('antigravity', {
                                        'event': 'agy_step',
                                        'step_kind': 'change',
                                        'step_id': sid_chg,
                                        'changes': [{'path': path, 'kind': {'type': 'update'}}],
                                        'completed': True,
                                    }, created_ts))

                            elif any(k in tname for k in ('read_file', 'view_file', 'search')):
                                path = args.get('Path') or args.get('query') or ''
                                is_read = 'read' in tname or 'view' in tname
                                sid_tool = f'agy-tool-{conv_id}-{idx}-{t_idx}'
                                if sid_tool not in known_step_ids:
                                    known_step_ids.add(sid_tool)
                                    pending_events.append(('antigravity', {
                                        'event': 'agy_step',
                                        'step_kind': 'tool',
                                        'step_id': sid_tool,
                                        'tool_kind': 'read' if is_read else 'search',
                                        'title': f'读取文件：{Path(path).name}' if is_read else f'联网搜索：{path}',
                                        'detail': path,
                                        'completed': True,
                                    }, created_ts))

        except OSError:
            return 0

        if not pending_events and next_synced_step == synced_step and new_offset == saved_offset:
            return 0

        with db.transaction() as conn:
            for kind, payload, created in pending_events:
                conn.execute(
                    'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?, NULL, ?, ?, ?)',
                    (session_id, kind, json.dumps(payload, ensure_ascii=False), created),
                )
            conn.execute(
                'UPDATE sessions SET antigravity_synced_step=?, antigravity_transcript_offset=? WHERE id=?',
                (next_synced_step, new_offset, session_id),
            )

        return len(pending_events)
