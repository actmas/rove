"""Two-way project, session metadata, and real-time rollout/history sync with local Codex."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import tomllib
import urllib.parse
import weakref

from . import store as db
from . import context_bridge
from .rpc import CodexRPC

VALID_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
SKIP_SUBPROJECT_NAMES = {'node_modules', '__pycache__', 'venv', '.venv', 'dist', 'build', 'target', 'runtime', 'tmp'}
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

_meta_lock = threading.Lock()
_session_locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_session_locks_guard = threading.Lock()
_sync_cache: dict[tuple[str, str], tuple[float, tuple[int, ...]]] = {}


def _get_session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def codex_home() -> Path | None:
    try:
        cfg = db.config()
    except OSError:
        cfg = {}
    configured = cfg.get('codex_home') or os.environ.get('WORKSTATION_CODEX_HOME')
    if configured:
        candidate = Path(configured).expanduser()
        return candidate.resolve() if candidate.is_dir() else None
    default_data = (Path.home() / '.config/walking-workstation').resolve()
    try:
        is_default_data = db.DATA.resolve() == default_data
    except OSError:
        is_default_data = False
    if not is_default_data:
        return None
    candidate = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser()
    return candidate.resolve() if candidate.is_dir() else None


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = f'file:{urllib.parse.quote(str(path))}?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _file_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _signature(home: Path) -> tuple[int, ...]:
    return (
        _file_mtime_ns(home / 'state_5.sqlite'),
        _file_mtime_ns(home / 'state_5.sqlite-wal'),
        _file_mtime_ns(home / 'config.toml'),
        _file_mtime_ns(home / '.codex-global-state.json'),
    )


def _clean_path_url(raw: str | None) -> str:
    if not raw:
        return ''
    if raw.startswith('file://'):
        return urllib.parse.unquote(raw.removeprefix('file://'))
    return raw


def _is_ephemeral_test_dir(path: Path) -> bool:
    parts = set(path.parts)
    return (
        'runtime' in parts
        or 'smoke-project' in parts
        or any(p.startswith('.') or p.startswith('workstation-') for p in path.parts)
    )


def _discover_codex_roots(home: Path) -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()

    def add(raw_path: str | None):
        if not raw_path:
            return
        with contextlib.suppress(OSError):
            p = Path(raw_path).expanduser().resolve()
            if p not in seen and p.is_dir() and not _is_ephemeral_test_dir(p):
                seen.add(p)
                discovered.append(p)

    config_toml = home / 'config.toml'
    if config_toml.is_file():
        with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
            parsed = tomllib.loads(config_toml.read_text(encoding='utf-8', errors='replace'))
            for raw_p, meta in (parsed.get('projects') or {}).items():
                if not isinstance(meta, dict) or meta.get('trust_level') in (None, 'trusted'):
                    add(raw_p)

    state_db = home / 'state_5.sqlite'
    if state_db.is_file():
        with contextlib.suppress(sqlite3.Error, OSError):
            with contextlib.closing(_ro_connect(state_db)) as conn:
                tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'project_roots' in tables:
                    for row in conn.execute('SELECT path FROM project_roots ORDER BY position'):
                        add(row['path'])

    global_state = home / '.codex-global-state.json'
    if global_state.is_file():
        with contextlib.suppress(OSError, ValueError):
            gstate = json.loads(global_state.read_text(encoding='utf-8', errors='replace'))
            for proj in (gstate.get('local-projects') or {}).values():
                if isinstance(proj, dict):
                    for rp in proj.get('rootPaths') or []:
                        add(rp)
            for key in ('active-workspace-roots', 'electron-saved-workspace-roots'):
                for rp in gstate.get(key) or []:
                    add(rp)

    return discovered


def _sync_allowed_roots_and_projects(home: Path) -> int:
    cfg = db.config()
    allowed: list[Path] = []
    for raw in cfg.get('allowed_roots', []):
        with contextlib.suppress(OSError):
            allowed.append(Path(raw).expanduser().resolve())

    discovered = _discover_codex_roots(home)
    changed_cfg = False
    for root in discovered:
        if not any(root == existing or root.is_relative_to(existing) for existing in allowed):
            allowed.append(root)
            cfg.setdefault('allowed_roots', []).append(str(root))
            changed_cfg = True

    if changed_cfg:
        db.write_private(db.DATA / 'config.json', json.dumps(cfg, ensure_ascii=False, indent=2))

    existing_projects = db.query('SELECT * FROM projects')
    by_path = {str(Path(p['path']).resolve()): p for p in existing_projects if Path(p['path']).exists()}
    used_names = {p['name'] for p in existing_projects}
    added = 0

    def ensure_project(folder: Path):
        nonlocal added
        resolved_str = str(folder.resolve())
        if resolved_str in by_path:
            return
        name = folder.name
        if name in used_names:
            name = f'{folder.parent.name}/{folder.name}'
        used_names.add(name)
        ident = secrets.token_hex(8)
        db.execute('INSERT OR IGNORE INTO projects(id,name,path) VALUES(?,?,?)', (ident, name, resolved_str))
        row = db.one('SELECT * FROM projects WHERE path=?', (resolved_str,))
        if row:
            by_path[resolved_str] = row
            added += 1

    # Register top-level allowed roots (Codex workspaces) and 1-level child repositories.
    for root in allowed:
        if not root.is_dir() or any(part.startswith('.') for part in root.parts):
            continue
        ensure_project(root)
        # Avoid flooding project list with date-named scratch folders under ~/Documents/Codex.
        with contextlib.suppress(OSError):
            for child in sorted(root.iterdir()):
                if (
                    not child.is_dir()
                    or child.is_symlink()
                    or child.name.startswith('.')
                    or child.name in SKIP_SUBPROJECT_NAMES
                    or DATE_DIR_RE.match(child.name)
                ):
                    continue
                ensure_project(child)

    return added


def _clean_thread_title(name: str | None, title: str | None, preview: str | None, first_msg: str | None) -> str:
    if name and name.strip():
        return re.sub(r'\s+', ' ', name.strip())[:100]
    for candidate in (title, preview, first_msg):
        if not candidate:
            continue
        for line in candidate.splitlines():
            cleaned = re.sub(r'\s+', ' ', line).strip()
            if cleaned:
                return cleaned[:80]
    return 'Codex 会话'


def _to_epoch_seconds(ts_val, ts_ms_val=None) -> float:
    if ts_ms_val:
        try:
            v = float(ts_ms_val)
            if v > 1e11:
                return v / 1000.0
        except (TypeError, ValueError):
            pass
    if ts_val:
        try:
            v = float(ts_val)
            return v / 1000.0 if v > 1e11 else v
        except (TypeError, ValueError):
            pass
    return time.time()


def _resolve_rollout_path(home: Path, raw_rollout: str | None) -> str | None:
    if not raw_rollout:
        return None
    p = Path(raw_rollout)
    if p.is_file():
        return str(p)
    # Handle relocated CODEX_HOME (e.g. in tests or migrated home directories).
    parts = p.parts
    if 'sessions' in parts:
        idx = parts.index('sessions')
        rel = Path(*parts[idx:])
        alt = home / rel
        if alt.is_file():
            return str(alt)
    return raw_rollout


def sync_projects_and_threads(force: bool = False, restore_dismissed: bool = False) -> dict:
    home = codex_home()
    if not home:
        return {'enabled': False, 'projects_added': 0, 'sessions_synced': 0}

    cache_key = (str(db.DATA), str(home))
    now_mono = time.monotonic()
    sig = _signature(home)
    if not force and not restore_dismissed:
        cached = _sync_cache.get(cache_key)
        if cached and (now_mono - cached[0] < 1.2 or cached[1] == sig):
            if cached[1] == sig and now_mono - cached[0] < 8.0:
                return {'enabled': True, 'cached': True}

    with _meta_lock:
        if restore_dismissed:
            db.execute('DELETE FROM dismissed_codex_threads')

        projects_added = _sync_allowed_roots_and_projects(home)
        state_db = home / 'state_5.sqlite'
        if not state_db.is_file():
            _sync_cache[cache_key] = (time.monotonic(), sig)
            return {'enabled': True, 'projects_added': projects_added, 'sessions_synced': 0}

        allowed_roots = [Path(r).resolve() for r in db.config().get('allowed_roots', []) if Path(r).exists()]
        all_projects = []
        for p in db.query('SELECT * FROM projects'):
            with contextlib.suppress(OSError):
                rp = Path(p['path']).resolve()
                if rp.is_dir() and any(rp == root or rp.is_relative_to(root) for root in allowed_roots):
                    all_projects.append((rp, p))
        # Sort longest path first so the most specific project directory wins.
        all_projects.sort(key=lambda item: len(item[0].parts), reverse=True)

        def match_project(raw_cwd: str | None):
            if not raw_cwd:
                return None, None
            with contextlib.suppress(OSError):
                cwd_p = Path(_clean_path_url(raw_cwd)).expanduser().resolve()
                if not any(cwd_p == root or cwd_p.is_relative_to(root) for root in allowed_roots):
                    return None, None
                for proj_path, proj_row in all_projects:
                    if cwd_p == proj_path or cwd_p.is_relative_to(proj_path):
                        valid_cwd = str(cwd_p) if cwd_p.is_dir() else str(proj_path)
                        return proj_row, valid_cwd
            return None, None

        try:
            with contextlib.closing(_ro_connect(state_db)) as sconn:
                tables = {r['name'] for r in sconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'threads' not in tables:
                    return {'enabled': True, 'projects_added': projects_added, 'sessions_synced': 0}
                child_ids = set()
                if 'thread_spawn_edges' in tables:
                    child_ids = {r['child_thread_id'] for r in sconn.execute('SELECT child_thread_id FROM thread_spawn_edges')}
                cols = {r['name'] for r in sconn.execute('PRAGMA table_info(threads)')}
                select_cols = [c for c in (
                    'id', 'rollout_path', 'created_at', 'updated_at', 'created_at_ms', 'updated_at_ms',
                    'source', 'thread_source', 'originator', 'agent_path', 'cwd', 'title', 'name',
                    'preview', 'first_user_message', 'archived', 'model', 'reasoning_effort',
                    'is_pinned', 'tokens_used', 'git_branch',
                ) if c in cols]
                thread_rows = [dict(r) for r in sconn.execute(
                    f"SELECT {','.join(select_cols)} FROM threads WHERE archived=0 ORDER BY updated_at DESC"
                )]
        except (sqlite3.Error, OSError):
            return {'enabled': True, 'projects_added': projects_added, 'sessions_synced': 0}

        dismissed = {r['native_id']: r['dismissed_at'] for r in db.query('SELECT * FROM dismissed_codex_threads')}
        existing_sessions = {}
        for r in db.query(
            "SELECT * FROM sessions WHERE native_codex_id IS NOT NULL OR (engine='codex' AND native_id IS NOT NULL)"
        ):
            cid = db.get_codex_native_id(r)
            if cid:
                existing_sessions[cid] = r

        synced_count = 0
        with db.transaction() as conn:
            for row in thread_rows:
                tid = row.get('id')
                if not tid:
                    continue
                source_str = row.get('source') or ''
                thread_source = row.get('thread_source')
                if (
                    tid in child_ids
                    or thread_source == 'subagent'
                    or row.get('agent_path')
                    or source_str.startswith('{"subagent"')
                ):
                    continue

                created_sec = _to_epoch_seconds(row.get('created_at'), row.get('created_at_ms'))
                updated_sec = _to_epoch_seconds(row.get('updated_at'), row.get('updated_at_ms'))

                if tid in dismissed and tid not in existing_sessions:
                    if updated_sec <= dismissed[tid] + 1.0:
                        continue
                    conn.execute('DELETE FROM dismissed_codex_threads WHERE native_id=?', (tid,))

                proj_row, resolved_cwd = match_project(row.get('cwd'))
                if not proj_row:
                    continue

                raw_title = row.get('title') or ''
                thread_kind = 'automation' if (thread_source == 'automation' or raw_title.startswith('Automation:')) else 'user'
                codex_name = (row.get('name') or '').strip() or None
                display_title = _clean_thread_title(
                    codex_name, raw_title, row.get('preview'), row.get('first_user_message')
                )
                model = (row.get('model') or '').strip() or None
                effort = row.get('reasoning_effort')
                if effort not in VALID_EFFORTS:
                    effort = None
                is_pin = int(bool(row.get('is_pinned')))
                tokens_used = int(row.get('tokens_used') or 0)
                git_branch = (row.get('git_branch') or '').strip() or None
                originator = (row.get('originator') or '').strip() or None
                rollout_path = _resolve_rollout_path(home, row.get('rollout_path'))

                existing = existing_sessions.get(tid)
                if existing:
                    next_title = existing['title']
                    next_codex_name = existing.get('codex_name')
                    if codex_name and codex_name != existing.get('codex_name'):
                        next_title = display_title
                        next_codex_name = codex_name
                    elif next_title in ('新会话', 'Codex 会话', '') and display_title:
                        next_title = display_title

                    prev_codex_pin = existing.get('codex_pinned')
                    if prev_codex_pin is None:
                        next_pin = max(int(bool(existing.get('pinned'))), is_pin)
                    elif is_pin != prev_codex_pin:
                        next_pin = is_pin
                    else:
                        next_pin = int(bool(existing.get('pinned')))

                    conn.execute(
                        '''UPDATE sessions
                           SET project_id=?, title=?, updated=?,
                               model=CASE WHEN engine='codex' THEN COALESCE(?, model) ELSE model END,
                               effort=CASE WHEN engine='codex' THEN COALESCE(?, effort) ELSE effort END, pinned=?, cwd=?, originator=COALESCE(?, originator),
                               thread_kind=?, git_branch=COALESCE(?, git_branch), tokens_used=MAX(tokens_used, ?),
                               rollout_path=COALESCE(?, rollout_path), codex_name=?, codex_pinned=?,
                               native_codex_id=COALESCE(native_codex_id, ?)
                           WHERE id=?''',
                        (
                            proj_row['id'],
                            next_title,
                            max(float(existing['updated']), updated_sec),
                            model,
                            effort,
                            next_pin,
                            resolved_cwd,
                            originator,
                            thread_kind,
                            git_branch,
                            tokens_used,
                            rollout_path,
                            next_codex_name,
                            is_pin,
                            tid,
                            existing['id'],
                        ),
                    )
                else:
                    sid = secrets.token_hex(12)
                    conn.execute(
                        '''INSERT OR IGNORE INTO sessions(
                               id, project_id, engine, last_engine, title, native_id, native_codex_id, created, updated,
                               model, effort, mode, fast, pinned, cwd, originator, thread_kind,
                               git_branch, tokens_used, rollout_path, codex_synced_ordinal,
                               codex_rollout_offset, codex_name, codex_pinned
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (
                            sid,
                            proj_row['id'],
                            'codex',
                            'codex',
                            display_title,
                            tid,
                            tid,
                            created_sec,
                            updated_sec,
                            model,
                            effort,
                            'standard',
                            0,
                            is_pin,
                            resolved_cwd,
                            originator,
                            thread_kind,
                            git_branch,
                            tokens_used,
                            rollout_path,
                            -1,
                            0,
                            codex_name,
                            is_pin,
                        ),
                    )
                synced_count += 1

        _sync_cache[cache_key] = (time.monotonic(), sig)
        return {'enabled': True, 'projects_added': projects_added, 'sessions_synced': synced_count}


def is_thread_locked(native_id: str | None, home: Path | None = None) -> bool:
    if not native_id:
        return False
    home = home or codex_home()
    if not home:
        return False
    lock_file = home / 'thread-writer-locks' / f'{native_id}.lock'
    if not lock_file.is_file():
        return False
    try:
        fd = os.open(str(lock_file), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _rollout_has_unfinished_turn(rollout_path: str | None) -> bool:
    if not rollout_path:
        return False
    rp = Path(rollout_path)
    try:
        st = rp.stat()
    except OSError:
        return False
    if time.time() - st.st_mtime > 1800:
        return False
    try:
        with rp.open('rb') as f:
            if st.st_size > 32768:
                f.seek(st.st_size - 32768)
            tail = f.read().decode('utf-8', errors='replace')
    except OSError:
        return False
    last_state = None
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        with contextlib.suppress(ValueError):
            obj = json.loads(line)
            if obj.get('type') == 'event_msg':
                et = (obj.get('payload') or {}).get('type')
                if et in ('task_started', 'task_complete', 'turn_aborted'):
                    last_state = et
    return last_state == 'task_started'


def external_activity_marker(native_id: str | None, rollout_path: str | None = None, home: Path | None = None) -> str:
    """Return a snapshot marker for an active Codex rollout/thread."""
    if not native_id:
        return ''
    home = home or codex_home()
    if home:
        resolved = _resolve_rollout_path(home, rollout_path)
        if resolved:
            resolved_path = Path(resolved)
            if resolved_path.is_file():
                with contextlib.suppress(OSError):
                    st = resolved_path.stat()
                    return f'codex:{st.st_size}:{st.st_mtime_ns}'
    return f'codex:{native_id}'


def mark_external_stopped(session_id: str, native_id: str, rollout_path: str | None = None, home: Path | None = None) -> None:
    """Record that the user explicitly stopped/unblocked an externally-active Codex thread from Rove."""
    marker = external_activity_marker(native_id, rollout_path, home) or f'codex:{native_id}'
    db.execute(
        'UPDATE sessions SET external_stopped_at=?, external_stopped_marker=? WHERE id=?',
        (time.time(), marker, session_id),
    )


def is_thread_running_externally(
    native_id: str | None,
    rollout_path: str | None = None,
    home: Path | None = None,
    session: dict | None = None,
) -> bool:
    if not native_id:
        return False
    home = home or codex_home()
    if not home:
        return False
    if not is_thread_locked(native_id, home):
        return False
    if session:
        stopped_marker = (session.get('external_stopped_marker') or '').strip()
        if stopped_marker and stopped_marker == external_activity_marker(native_id, rollout_path, home):
            return False
    th_db = home / 'thread_history_1.sqlite'
    if th_db.is_file():
        with contextlib.suppress(sqlite3.Error, OSError):
            with contextlib.closing(_ro_connect(th_db)) as conn:
                tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'thread_turns' in tables:
                    row = conn.execute(
                        "SELECT 1 FROM thread_turns WHERE thread_id=? AND (completed_at IS NULL OR status IN ('inProgress','running')) LIMIT 1",
                        (native_id,),
                    ).fetchone()
                    if row:
                        return True
    return _rollout_has_unfinished_turn(_resolve_rollout_path(home, rollout_path))


def live_thread_states(sessions: list[dict], active_local_session_ids: set[str]) -> dict[str, dict]:
    home = codex_home()
    if not home:
        return {}

    lock_dir = home / 'thread-writer-locks'
    locked_native_ids: set[str] = set()
    if lock_dir.is_dir():
        wanted = {db.get_codex_native_id(s) for s in sessions if db.get_codex_native_id(s)}
        with contextlib.suppress(OSError):
            for lf in lock_dir.glob('*.lock'):
                tid = lf.stem
                if tid in wanted and is_thread_locked(tid, home):
                    locked_native_ids.add(tid)

    in_progress_turns: set[str] = set()
    th_db = home / 'thread_history_1.sqlite'
    if th_db.is_file():
        with contextlib.suppress(sqlite3.Error, OSError):
            with contextlib.closing(_ro_connect(th_db)) as conn:
                tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'thread_turns' in tables:
                    for r in conn.execute(
                        "SELECT DISTINCT thread_id FROM thread_turns WHERE completed_at IS NULL OR status IN ('inProgress','running')"
                    ):
                        in_progress_turns.add(r['thread_id'])

    states: dict[str, dict] = {}
    for s in sessions:
        tid = db.get_codex_native_id(s)
        if not tid or s['id'] in active_local_session_ids:
            continue
        locked = tid in locked_native_ids
        if not locked:
            continue
        running = tid in in_progress_turns
        if not running:
            running = _rollout_has_unfinished_turn(_resolve_rollout_path(home, s.get('rollout_path')))
        if running:
            stopped_marker = (s.get('external_stopped_marker') or '').strip()
            if stopped_marker and stopped_marker == external_activity_marker(tid, s.get('rollout_path'), home):
                continue
            states[s['id']] = {'external_active': True, 'writer_locked': locked}
    return states


def _extract_user_text(content_list) -> str:
    if not isinstance(content_list, list):
        return ''
    texts = []
    images = 0
    for part in content_list:
        if not isinstance(part, dict):
            continue
        ptype = part.get('type')
        if ptype in ('text', 'input_text', 'Text'):
            val = part.get('text') or ''
            if val:
                texts.append(val)
        elif ptype in ('localImage', 'image', 'input_image'):
            images += 1
        elif ptype == 'mention' and part.get('path'):
            texts.append(f"`{part['path']}`")
    joined = context_bridge.strip_injected_context('\n'.join(texts).strip())
    if not joined and images:
        return f'[本机附加了 {images} 张图片]'
    return joined


def _normalize_v2_item(item_obj: dict) -> tuple[str, dict] | None:
    """Convert a thread_history_1.sqlite camelCase item_json into a workstation event tuple (kind, payload)."""
    itype = item_obj.get('type')
    item_id = item_obj.get('id')
    if not itype or not item_id:
        return None

    if itype == 'userMessage':
        text = _extract_user_text(item_obj.get('content'))
        if not text:
            return None
        return 'user', {'text': text, 'item_id': item_id, 'source': 'codex_sync'}

    if itype == 'reasoning':
        summary = [s for s in (item_obj.get('summary') or []) if s]
        content = [c for c in (item_obj.get('content') or []) if c]
        if not summary and not content:
            return None
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'reasoning',
            'id': item_id,
            'summary': summary,
            'content': content,
        }}}

    if itype == 'commandExecution':
        cmd = item_obj.get('command')
        if isinstance(cmd, list):
            cmd = cmd[2] if len(cmd) >= 3 and cmd[1] == '-lc' else ' '.join(str(x) for x in cmd)
        normalized = {
            **item_obj,
            'type': 'commandExecution',
            'command': cmd or '',
            'cwd': _clean_path_url(item_obj.get('cwd')),
        }
        return 'codex', {'method': 'item/completed', 'params': {'item': normalized}}

    if itype in (
        'agentMessage', 'fileChange', 'webSearch', 'mcpToolCall', 'collabAgentToolCall',
        'plan', 'subAgentActivity', 'imageView', 'imageGeneration', 'contextCompaction',
    ):
        normalized = dict(item_obj)
        if itype == 'imageView' and normalized.get('path'):
            normalized['path'] = _clean_path_url(normalized['path'])
        return 'codex', {'method': 'item/completed', 'params': {'item': normalized}}

    return None


def _normalize_raw_rollout_item(raw_item: dict) -> tuple[str, dict] | None:
    """Convert a raw rollout-*.jsonl event_msg item_completed payload into normalized v2 event."""
    rtype = raw_item.get('type')
    item_id = raw_item.get('id')
    if not rtype or not item_id:
        return None

    if rtype == 'UserMessage':
        text = _extract_user_text(raw_item.get('content'))
        if not text:
            return None
        return 'user', {'text': text, 'item_id': item_id, 'source': 'codex_sync'}

    if rtype == 'AgentMessage':
        parts = [c.get('text', '') for c in (raw_item.get('content') or []) if isinstance(c, dict) and c.get('text')]
        text = ''.join(parts) if parts else (raw_item.get('text') or '')
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'agentMessage',
            'id': item_id,
            'text': text,
            'phase': raw_item.get('phase'),
        }}}

    if rtype == 'Reasoning':
        summary = [s for s in (raw_item.get('summary_text') or raw_item.get('summary') or []) if s]
        content = [c for c in (raw_item.get('raw_content') or raw_item.get('content') or []) if c]
        if not summary and not content:
            return None
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'reasoning',
            'id': item_id,
            'summary': summary,
            'content': content,
        }}}

    if rtype == 'CommandExecution':
        cmd = raw_item.get('command')
        if isinstance(cmd, list):
            cmd = cmd[2] if len(cmd) >= 3 and cmd[1] == '-lc' else ' '.join(str(x) for x in cmd)
        dur = raw_item.get('duration')
        dur_ms = raw_item.get('durationMs')
        if dur_ms is None and isinstance(dur, dict):
            dur_ms = int((dur.get('secs') or 0) * 1000 + (dur.get('nanos') or 0) / 1_000_000)
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'commandExecution',
            'id': item_id,
            'command': cmd or '',
            'cwd': _clean_path_url(raw_item.get('cwd')),
            'aggregatedOutput': raw_item.get('aggregated_output') or raw_item.get('aggregatedOutput') or '',
            'exitCode': raw_item.get('exit_code') if 'exit_code' in raw_item else raw_item.get('exitCode'),
            'durationMs': dur_ms,
            'status': raw_item.get('status') or 'completed',
        }}}

    if rtype == 'FileChange':
        raw_changes = raw_item.get('changes') or {}
        changes_list = []
        if isinstance(raw_changes, dict):
            for fpath, ch in raw_changes.items():
                if not isinstance(ch, dict):
                    continue
                ctype = ch.get('type') or 'update'
                diff_text = ch.get('unified_diff') or ch.get('diff') or ''
                if not diff_text and ctype == 'add' and ch.get('content'):
                    diff_text = '\n'.join('+' + ln for ln in str(ch['content']).splitlines()[:160])
                changes_list.append({
                    'path': _clean_path_url(fpath),
                    'kind': {'type': ctype},
                    'diff': diff_text[:16000],
                })
        elif isinstance(raw_changes, list):
            changes_list = raw_changes
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'fileChange',
            'id': item_id,
            'status': 'completed',
            'changes': changes_list,
        }}}

    if rtype == 'McpToolCall':
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'mcpToolCall',
            'id': item_id,
            'server': raw_item.get('server') or 'mcp',
            'tool': raw_item.get('tool') or '',
            'arguments': raw_item.get('arguments'),
            'status': raw_item.get('status') or 'completed',
        }}}

    if rtype == 'Extension':
        kind = raw_item.get('kind') or ''
        if kind == 'web.search':
            return 'codex', {'method': 'item/completed', 'params': {'item': {
                'type': 'webSearch',
                'id': item_id,
                'query': raw_item.get('query') or '',
            }}}
        return None

    if rtype == 'ImageView':
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'imageView',
            'id': item_id,
            'path': _clean_path_url(raw_item.get('path')),
        }}}

    if rtype == 'ContextCompaction':
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'contextCompaction',
            'id': item_id,
        }}}

    if rtype == 'Plan':
        return 'codex', {'method': 'item/completed', 'params': {'item': {
            'type': 'plan',
            'id': item_id,
            'text': raw_item.get('text') or '',
        }}}

    # Also support already-camelCase items if present in newer rollout formats.
    if rtype[0].islower():
        return _normalize_v2_item(raw_item)
    return None


def _token_event_from_rollout(payload: dict) -> dict | None:
    info = payload.get('info')
    if not isinstance(info, dict):
        return None
    total = info.get('total_token_usage') or {}
    last = info.get('last_token_usage') or {}
    return {
        'method': 'thread/tokenUsage/updated',
        'params': {
            'tokenUsage': {
                'total': {
                    'totalTokens': int(total.get('total_tokens') or 0),
                    'reasoningOutputTokens': int(total.get('reasoning_output_tokens') or 0),
                },
                'last': {
                    'totalTokens': int(last.get('total_tokens') or 0),
                },
                'modelContextWindow': info.get('model_context_window'),
            }
        },
    }


def sync_session_events(session_id: str) -> int:
    home = codex_home()
    if not home:
        return 0

    lock = _get_session_lock(session_id)
    with lock:
        s = db.one('SELECT * FROM sessions WHERE id=?', (session_id,))
        tid = db.get_codex_native_id(s)
        if not s or not tid:
            return 0

        # Do not race against our own live CodexRPC worker streaming real-time deltas.
        active_job = db.one(
            "SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting')",
            (session_id,),
        )
        if active_job:
            return 0

        th_db = home / 'thread_history_1.sqlite'
        rollout_path_str = _resolve_rollout_path(home, s.get('rollout_path'))
        rp = Path(rollout_path_str) if rollout_path_str else None
        if not th_db.is_file() and not (rp and rp.is_file()):
            return 0

        synced_ordinal = int(s.get('codex_synced_ordinal') if s.get('codex_synced_ordinal') is not None else -1)
        rollout_offset = int(s.get('codex_rollout_offset') or 0)

        # Bootstrap synced_codex_items from any pre-existing events in workstation DB.
        existing_user_texts: set[str] = set()
        for r in db.query("SELECT payload FROM events WHERE session_id=? AND kind='user'", (session_id,)):
            with contextlib.suppress(Exception):
                t = context_bridge.strip_injected_context((json.loads(r['payload']).get('text') or '').strip())
                if t:
                    existing_user_texts.add(t)
        has_token_event = False
        if synced_ordinal < 0:
            prior_events = db.query('SELECT kind, payload FROM events WHERE session_id=? ORDER BY id', (session_id,))
            if prior_events:
                with db.transaction() as conn:
                    for ev in prior_events:
                        with contextlib.suppress(ValueError):
                            p = json.loads(ev['payload'])
                            if ev['kind'] == 'user' and p.get('text'):
                                if p.get('item_id'):
                                    conn.execute(
                                        'INSERT OR IGNORE INTO synced_codex_items(session_id,item_id,updated_ordinal) VALUES(?,?,?)',
                                        (session_id, p['item_id'], 999999999),
                                    )
                            elif ev['kind'] == 'codex':
                                if p.get('method') == 'thread/tokenUsage/updated':
                                    has_token_event = True
                                item = (p.get('params') or {}).get('item')
                                if isinstance(item, dict) and item.get('id'):
                                    conn.execute(
                                        'INSERT OR IGNORE INTO synced_codex_items(session_id,item_id,updated_ordinal) VALUES(?,?,?)',
                                        (session_id, item['id'], 999999999),
                                    )
        else:
            has_token_event = bool(
                db.one(
                    "SELECT 1 FROM events WHERE session_id=? AND kind='codex' AND payload LIKE '%tokenUsage%' LIMIT 1",
                    (session_id,),
                )
            )

        known_items = {
            r['item_id']: int(r['updated_ordinal'])
            for r in db.query('SELECT item_id, updated_ordinal FROM synced_codex_items WHERE session_id=?', (session_id,))
        }

        pending_events: list[tuple[str, dict, float]] = []
        item_updates: list[tuple[str, int]] = []
        next_synced_ordinal = synced_ordinal
        projected_byte_offset = 0
        has_history_db_projection = False

        # 1. Read projected normalized items from thread_history_1.sqlite.
        th_db = home / 'thread_history_1.sqlite'
        if th_db.is_file():
            with contextlib.suppress(sqlite3.Error, OSError):
                with contextlib.closing(_ro_connect(th_db)) as thconn:
                    tables = {r['name'] for r in thconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'thread_history_projection_state' in tables:
                        proj = thconn.execute(
                            'SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?',
                            (tid,),
                        ).fetchone()
                        if proj:
                            projected_byte_offset = int(proj['next_rollout_byte_offset'] or 0)
                            has_history_db_projection = True

                    if 'thread_items' in tables:
                        rows = thconn.execute(
                            '''SELECT item_id, item_type, item_json, rollout_ordinal, updated_at_ordinal, created_at_ms
                               FROM thread_items
                               WHERE thread_id=? AND updated_at_ordinal>?
                               ORDER BY rollout_ordinal ASC, updated_at_ordinal ASC''',
                            (tid, synced_ordinal),
                        ).fetchall()
                        for r in rows:
                            iid = r['item_id']
                            uord = int(r['updated_at_ordinal'] or 0)
                            next_synced_ordinal = max(next_synced_ordinal, uord)
                            if iid in known_items and (known_items[iid] >= uord or r['item_type'] == 'userMessage'):
                                continue
                            try:
                                item_obj = json.loads(r['item_json'])
                            except (TypeError, ValueError):
                                continue
                            converted = _normalize_v2_item(item_obj)
                            known_items[iid] = uord
                            item_updates.append((iid, uord))
                            if not converted:
                                continue
                            kind, payload = converted
                            if kind == 'user' and payload['text'].strip() in existing_user_texts:
                                existing_user_texts.discard(payload['text'].strip())
                                continue
                            created = _to_epoch_seconds(None, r['created_at_ms'])
                            pending_events.append((kind, payload, created))

        # 2. Tail rollout_path for live unprojected items and token usage updates.
        rollout_path_str = _resolve_rollout_path(home, s.get('rollout_path'))
        next_rollout_offset = rollout_offset
        latest_token_payload = None

        if rollout_path_str:
            rp = Path(rollout_path_str)
            if rp.is_file():
                with contextlib.suppress(OSError):
                    fsize = rp.stat().st_size
                    # If history DB projected items, only parse items after projected_byte_offset,
                    # unless we also need initial token_count from the file tail.
                    item_parse_start = max(projected_byte_offset, rollout_offset) if has_history_db_projection else rollout_offset
                    if fsize > item_parse_start:
                        with rp.open('rb') as f:
                            f.seek(item_parse_start)
                            chunk = f.read(fsize - item_parse_start)
                        last_nl = chunk.rfind(b'\n')
                        if last_nl >= 0:
                            complete_bytes = chunk[: last_nl + 1]
                            next_rollout_offset = item_parse_start + last_nl + 1
                            for raw_line in complete_bytes.decode('utf-8', errors='replace').splitlines():
                                raw_line = raw_line.strip()
                                if not raw_line.startswith('{'):
                                    continue
                                try:
                                    obj = json.loads(raw_line)
                                except ValueError:
                                    continue
                                if obj.get('type') != 'event_msg':
                                    continue
                                p = obj.get('payload') or {}
                                et = p.get('type')
                                ord_val = int(obj.get('ordinal') or 0)
                                if et == 'token_count':
                                    tok = _token_event_from_rollout(p)
                                    if tok:
                                        latest_token_payload = tok
                                elif et == 'item_completed' and isinstance(p.get('item'), dict):
                                    raw_it = p['item']
                                    iid = raw_it.get('id')
                                    if not iid or iid in known_items:
                                        continue
                                    converted = _normalize_raw_rollout_item(raw_it)
                                    known_items[iid] = 999999999
                                    item_updates.append((iid, 999999999))
                                    if not converted:
                                        continue
                                    kind, payload = converted
                                    if kind == 'user' and payload['text'].strip() in existing_user_texts:
                                        existing_user_texts.discard(payload['text'].strip())
                                        continue
                                    pending_events.append((kind, payload, time.time()))
                    elif fsize > rollout_offset:
                        # History DB already projected up to fsize; grab latest token_count from tail once.
                        if not has_token_event:
                            with rp.open('rb') as f:
                                if fsize > 65536:
                                    f.seek(fsize - 65536)
                                tail_text = f.read().decode('utf-8', errors='replace')
                            for raw_line in reversed(tail_text.splitlines()):
                                if '"token_count"' in raw_line:
                                    with contextlib.suppress(ValueError):
                                        obj = json.loads(raw_line)
                                        if obj.get('type') == 'event_msg' and (obj.get('payload') or {}).get('type') == 'token_count':
                                            latest_token_payload = _token_event_from_rollout(obj['payload'])
                                            if latest_token_payload:
                                                break
                        next_rollout_offset = fsize

        if latest_token_payload:
            pending_events.append(('codex', latest_token_payload, time.time()))
            has_token_event = True
        elif not has_token_event and int(s.get('tokens_used') or 0) > 0 and (pending_events or synced_ordinal < 0):
            pending_events.append(('codex', {
                'method': 'thread/tokenUsage/updated',
                'params': {
                    'tokenUsage': {
                        'total': {'totalTokens': int(s['tokens_used']), 'reasoningOutputTokens': 0},
                        'last': {'totalTokens': 0},
                        'modelContextWindow': None,
                    }
                },
            }, float(s.get('updated') or time.time())))

        if synced_ordinal < 0 and next_synced_ordinal < 0:
            next_synced_ordinal = 0

        if not pending_events and not item_updates and next_synced_ordinal == synced_ordinal and next_rollout_offset == rollout_offset:
            return 0

        with db.transaction() as conn:
            for iid, uord in item_updates:
                conn.execute(
                    'INSERT OR REPLACE INTO synced_codex_items(session_id,item_id,updated_ordinal) VALUES(?,?,?)',
                    (session_id, iid, uord),
                )
            for kind, payload, created in pending_events:
                conn.execute(
                    'INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,NULL,?,?,?)',
                    (session_id, kind, json.dumps(payload, ensure_ascii=False), created),
                )
            conn.execute(
                'UPDATE sessions SET codex_synced_ordinal=?, codex_rollout_offset=?, rollout_path=COALESCE(?, rollout_path) WHERE id=?',
                (next_synced_ordinal, next_rollout_offset, rollout_path_str, session_id),
            )
        return len(pending_events)


async def push_codex_thread_name(native_id: str | None, name: str):
    if not native_id or not name:
        return
    try:
        codex_bin = db.config().get('codex_bin')
    except OSError:
        return
    if not codex_bin or not Path(codex_bin).is_file() or is_thread_locked(native_id):
        return

    import asyncio

    async def noop_notify(_m, _p):
        pass

    async def noop_req(_msg):
        return {'error': {'code': -32601, 'message': 'unsupported'}}

    rpc = CodexRPC(codex_bin, noop_notify, noop_req)
    try:
        await asyncio.wait_for(rpc.start(), timeout=4.0)
        await asyncio.wait_for(rpc.call('thread/name/set', {'threadId': native_id, 'name': name}), timeout=4.0)
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            await rpc.close()
