import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
import weakref

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('WORKSTATION_DATA', Path.home() / '.config/walking-workstation'))

_local = threading.local()
_config_cache: tuple[str, float, dict] | None = None
_config_lock = threading.Lock()
_last_reconcile_ts: float = 0.0
_reconcile_lock = threading.Lock()
_open_connections: set[sqlite3.Connection] = set()
_open_conns_lock = threading.Lock()


def _close_conn(conn: sqlite3.Connection):
    with _open_conns_lock:
        _open_connections.discard(conn)
    with contextlib.suppress(Exception):
        conn.close()


def connect():
    db = sqlite3.connect(DATA / 'state.sqlite3', timeout=15, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA temp_store=MEMORY')
    return db


def close_thread_connections():
    """Explicitly close thread-local connection for clean teardown."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        _local.conn = None
        _local.conn_path = None
        _close_conn(conn)


def close_all_connections():
    """Explicitly close all open tracked connections across all threads."""
    with _open_conns_lock:
        conns = list(_open_connections)
        _open_connections.clear()
    for conn in conns:
        with contextlib.suppress(Exception):
            conn.close()
    _local.conn = None
    _local.conn_path = None


def _thread_conn():
    db_file = str(DATA / 'state.sqlite3')
    conn = getattr(_local, 'conn', None)
    conn_path = getattr(_local, 'conn_path', None)
    if conn is not None and conn_path == db_file:
        try:
            conn.execute('SELECT 1')
            return conn
        except sqlite3.Error:
            _close_conn(conn)
    if conn is not None:
        _close_conn(conn)
    conn = connect()
    with _open_conns_lock:
        _open_connections.add(conn)
    _local.conn = conn
    _local.conn_path = db_file
    cur_thread = threading.current_thread()
    # If this is a worker thread (e.g. from asyncio.to_thread or threadpool), auto-close when the thread object is GCed
    if cur_thread is not threading.main_thread():
        weakref.finalize(cur_thread, _close_conn, conn)
    return conn


@contextlib.contextmanager
def transaction():
    conn = _thread_conn()
    if conn.in_transaction:
        sp_id = f"sp_{int(time.time() * 1000000)}_{secrets.token_hex(4)}"
        conn.execute(f"SAVEPOINT {sp_id}")
        try:
            yield conn
            conn.execute(f"RELEASE SAVEPOINT {sp_id}")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp_id}")
                conn.execute(f"RELEASE SAVEPOINT {sp_id}")
            raise
    else:
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise


def query(sql, args=()):
    db = _thread_conn()
    return [dict(r) for r in db.execute(sql, args)]


def one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    db = _thread_conn()
    return db.execute(sql, args).lastrowid


def decide_approval(ident, decision, event_key=None):
    """Commit a pending decision and its optional channel delivery atomically."""
    with transaction() as conn:
        if event_key and conn.execute('SELECT 1 FROM channel_events WHERE id=?', (event_key,)).fetchone():
            return False
        updated = conn.execute(
            'UPDATE approvals SET decision=? WHERE id=? AND decision IS NULL '
            "AND EXISTS (SELECT 1 FROM jobs WHERE jobs.id=approvals.job_id AND jobs.status='waiting')",
            (decision, ident),
        )
        if not updated.rowcount:
            return False
        if event_key:
            conn.execute('INSERT INTO channel_events(id,created) VALUES(?,?)', (event_key, time.time()))
        return True


def config():
    global _config_cache
    cfg_path = DATA / 'config.json'
    path_str = str(cfg_path)
    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        mtime = -1.0
    cached = _config_cache
    if cached is not None and cached[0] == path_str and cached[1] == mtime:
        return cached[2]
    with _config_lock:
        raw = json.loads(cfg_path.read_text(encoding='utf-8'))
        _config_cache = (path_str, mtime, raw)
        return raw


def write_private(path, value):
    tmp = path.with_suffix('.tmp-' + secrets.token_hex(4))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(value)
    os.replace(tmp, path)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def _ensure_columns(db, table, definitions):
    existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
    for name, ddl in definitions.items():
        if name not in existing:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')


def _detect_lan_ip() -> str | None:
    import socket
    env_ip = os.environ.get('ROVE_LAN_IP', '').strip()
    if env_ip:
        return env_ip
    with contextlib.suppress(Exception):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return ip
    return None


def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    if not (DATA / 'config.json').exists():
        import shutil
        import socket
        import sys
        from urllib.parse import urlparse
        workspace = ROOT.parent
        roots = [str(workspace)]
        env_roots = os.environ.get('ROVE_ALLOWED_ROOTS', '').strip()
        if env_roots:
            for item in env_roots.split(os.pathsep):
                p = Path(item).expanduser()
                if p.is_dir() and str(p) not in roots:
                    roots.append(str(p))
        elif sys.platform == 'darwin':
            for candidate in (Path.home() / 'Projects', Path.home() / 'Documents/Projects', Path.home() / 'Documents/Codex'):
                if candidate.is_dir() and str(candidate) not in roots:
                    roots.append(str(candidate))
        else:
            pve_ws = DATA / 'workspaces'
            pve_ws.mkdir(parents=True, exist_ok=True, mode=0o700)
            roots.append(str(pve_ws))
            opt_ws = Path('/var/lib/walking-workstation/workspaces')
            if opt_ws.is_dir() and str(opt_ws) not in roots:
                roots.append(str(opt_ws))
        default_codex = (
            shutil.which('codex')
            or ('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
                if sys.platform == 'darwin' else str(Path.home() / '.local/bin/codex'))
        )
        default_agy = shutil.which('agy') or str(Path.home() / '.local/bin/agy')
        port = os.environ.get('WORKSTATION_PORT', '8765').strip() or '8765'
        hostname = socket.gethostname()
        lan_ip = _detect_lan_ip()
        public_url = os.environ.get('ROVE_PUBLIC_URL', '').strip().rstrip('/')
        hosts = ['localhost', '127.0.0.1']
        origins = [f'http://localhost:{port}', f'http://127.0.0.1:{port}']
        if lan_ip and lan_ip not in hosts:
            hosts.append(lan_ip)
            origins.append(f'http://{lan_ip}:{port}')
        if hostname and hostname not in hosts:
            hosts.append(hostname)
        if public_url:
            parsed_pub = urlparse(public_url)
            if parsed_pub.hostname and parsed_pub.hostname not in hosts:
                hosts.append(parsed_pub.hostname)
            if public_url not in origins:
                origins.append(public_url)
        cfg = {'allowed_roots': roots,
               'allowed_hosts': hosts,
               'origins': origins,
               'lan_url': f'http://{lan_ip}:{port}' if lan_ip else '',
               'public_base_url': public_url,
               'codex_bin': default_codex,
               'antigravity_bin': default_agy}
        write_private(DATA / 'config.json', json.dumps(cfg, ensure_ascii=False, indent=2))
    if not (DATA / 'auth.json').exists():
        password, salt = secrets.token_urlsafe(18), secrets.token_hex(16)
        write_private(DATA / 'auth.json', json.dumps({'salt': salt, 'hash': password_hash(password, salt)}))
        write_private(DATA / 'access-password.txt', password + '\n')
    with contextlib.closing(connect()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        DROP TABLE IF EXISTS live_activity_tokens;
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), engine TEXT NOT NULL,
            title TEXT NOT NULL, native_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
            model TEXT, effort TEXT, mode TEXT NOT NULL DEFAULT 'standard', fast INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
            cwd TEXT, originator TEXT, thread_kind TEXT NOT NULL DEFAULT 'user', git_branch TEXT,
            tokens_used INTEGER NOT NULL DEFAULT 0, rollout_path TEXT,
            codex_synced_ordinal INTEGER NOT NULL DEFAULT -1, codex_rollout_offset INTEGER NOT NULL DEFAULT 0,
            codex_name TEXT, codex_pinned INTEGER);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT UNIQUE NOT NULL,
            prompt TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER, started REAL, ended REAL, stop INTEGER NOT NULL DEFAULT 0, error TEXT,
            model TEXT, effort TEXT, mode TEXT, fast INTEGER NOT NULL DEFAULT 0, images TEXT NOT NULL DEFAULT '[]');
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(session_id) WHERE status IN ('queued','running','waiting');
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
            job_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS event_session ON events(session_id,id);
        CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_project_updated ON sessions(project_id, updated DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_native_id ON sessions(native_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_session_started ON jobs(session_id, started DESC);
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), method TEXT NOT NULL,
            payload TEXT NOT NULL, decision TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS terminals(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            status TEXT NOT NULL, pid INTEGER, created REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_terminal ON terminals(session_id) WHERE status IN ('starting','running');
        CREATE TABLE IF NOT EXISTS terminal_output(id INTEGER PRIMARY KEY AUTOINCREMENT, terminal_id TEXT NOT NULL REFERENCES terminals(id), data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS terminal_output_id ON terminal_output(terminal_id,id);
        CREATE TABLE IF NOT EXISTS terminal_input(id INTEGER PRIMARY KEY AUTOINCREMENT, terminal_id TEXT NOT NULL REFERENCES terminals(id), kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logins(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS engine_checks(engine TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS engine_models(engine TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS synced_codex_items(
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,
            updated_ordinal INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(session_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS dismissed_codex_threads(
            native_id TEXT PRIMARY KEY,
            dismissed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dismissed_antigravity_threads(
            native_id TEXT PRIMARY KEY,
            dismissed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions(
            id TEXT PRIMARY KEY,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            device_label TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            last_success REAL,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS channel_events(
            id TEXT PRIMARY KEY,
            created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_memories(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            category TEXT NOT NULL DEFAULT 'convention',
            content TEXT NOT NULL,
            source_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_project_memories_proj ON project_memories(project_id, pinned DESC, updated DESC);
        CREATE TABLE IF NOT EXISTS quota_waits(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            job_id TEXT NOT NULL,
            engine TEXT NOT NULL,
            model TEXT,
            prompt TEXT NOT NULL,
            has_output INTEGER NOT NULL DEFAULT 0,
            reset_at REAL NOT NULL,
            resume_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'waiting',
            reason TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_quota_waits_due ON quota_waits(status, resume_at);
        CREATE TABLE IF NOT EXISTS reviews(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            review_job_id TEXT,
            reviewer_engine TEXT NOT NULL,
            reviewer_model TEXT,
            git_head TEXT NOT NULL DEFAULT '',
            workspace_hash TEXT NOT NULL DEFAULT '',
            focus TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            findings_count INTEGER,
            raw_output TEXT NOT NULL DEFAULT '',
            applied INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_session ON reviews(session_id, created DESC);
        CREATE TABLE IF NOT EXISTS secret_handoffs(
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL DEFAULT '',
            origin_label TEXT NOT NULL DEFAULT '',
            fields_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            steps INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_secret_handoffs_job ON secret_handoffs(job_id, status);
        CREATE TABLE IF NOT EXISTS subscription_accounts(
            id TEXT PRIMARY KEY,
            engine TEXT NOT NULL,
            label TEXT NOT NULL,
            home_dir TEXT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub_accounts_engine ON subscription_accounts(engine, paused, priority);
        CREATE TABLE IF NOT EXISTS session_queue(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            prompt TEXT NOT NULL,
            engine TEXT,
            model TEXT,
            effort TEXT,
            mode TEXT,
            fast INTEGER,
            images TEXT NOT NULL DEFAULT '[]',
            steer INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_queue_sess ON session_queue(session_id, status, steer DESC, created ASC);
        CREATE TABLE IF NOT EXISTS worktree_recycle_journal(
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'waiting',
            reason TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhook_deliveries(
            delivery_id TEXT PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            received_at REAL NOT NULL
        );
        ''')
        _ensure_columns(db, 'project_memories', {
            'why': "TEXT NOT NULL DEFAULT ''",
            'how_to_apply': "TEXT NOT NULL DEFAULT ''",
        })
        _ensure_columns(db, 'sessions', {
            'pending_runtime_json': "TEXT NOT NULL DEFAULT '{}'",
            'runtime_generation': 'INTEGER NOT NULL DEFAULT 1',
            'parent_session_id': 'TEXT',
            'forked_at_job_id': 'TEXT',
            'account_id': 'TEXT',
        })
        with contextlib.suppress(sqlite3.OperationalError):
            db.execute(
                'CREATE VIRTUAL TABLE IF NOT EXISTS project_memories_fts '
                'USING fts5(memory_id UNINDEXED, project_id UNINDEXED, category, content, why, how_to_apply)'
            )
        db.execute('''
            CREATE TABLE IF NOT EXISTS arena_runs(
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                prompt TEXT NOT NULL,
                session_a_id TEXT NOT NULL,
                session_b_id TEXT NOT NULL,
                engine_a TEXT NOT NULL,
                engine_b TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                winner_session_id TEXT,
                created REAL NOT NULL,
                updated REAL NOT NULL
            )
        ''')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS orchestration_runs(
                id TEXT PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                prompt TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'relay',
                status TEXT NOT NULL DEFAULT 'planning',
                plan_version INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                policy_json TEXT NOT NULL DEFAULT '{}',
                budget_json TEXT NOT NULL DEFAULT '{}',
                baseline_ref TEXT NOT NULL DEFAULT '',
                workspace_path TEXT NOT NULL DEFAULT '',
                fix_rounds INTEGER NOT NULL DEFAULT 0,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                plan_retries INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at REAL NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 1,
                wait_reason TEXT NOT NULL DEFAULT '',
                terminal_reason TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orch_runs_sess ON orchestration_runs(root_session_id, updated DESC);
            CREATE INDEX IF NOT EXISTS idx_orch_runs_status ON orchestration_runs(status, updated DESC);

            CREATE TABLE IF NOT EXISTS orchestration_steps(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES orchestration_runs(id) ON DELETE CASCADE,
                plan_version INTEGER NOT NULL DEFAULT 1,
                step_index INTEGER NOT NULL DEFAULT 0,
                logical_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                files_scope_json TEXT NOT NULL DEFAULT '[]',
                acceptance_json TEXT NOT NULL DEFAULT '[]',
                required_capabilities_json TEXT NOT NULL DEFAULT '[]',
                prompt TEXT NOT NULL DEFAULT '',
                optional INTEGER NOT NULL DEFAULT 0,
                preferred_engine TEXT,
                preferred_model TEXT,
                preferred_effort TEXT,
                active_attempt_id TEXT,
                sub_session_id TEXT,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(run_id, plan_version, logical_key)
            );
            CREATE INDEX IF NOT EXISTS idx_orch_steps_run ON orchestration_steps(run_id, plan_version, step_index);

            CREATE TABLE IF NOT EXISTS orchestration_attempts(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES orchestration_runs(id) ON DELETE CASCADE,
                step_id TEXT NOT NULL REFERENCES orchestration_steps(id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                job_id TEXT UNIQUE NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1,
                engine TEXT NOT NULL,
                model TEXT,
                effort TEXT,
                route_reason TEXT NOT NULL DEFAULT '',
                capability_snapshot_json TEXT NOT NULL DEFAULT '{}',
                input_digest TEXT NOT NULL DEFAULT '',
                output_digest TEXT NOT NULL DEFAULT '',
                started REAL NOT NULL,
                ended REAL,
                outcome TEXT,
                error_class TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                UNIQUE(step_id, attempt_no)
            );
            CREATE INDEX IF NOT EXISTS idx_orch_attempts_run ON orchestration_attempts(run_id, started DESC);

            CREATE TABLE IF NOT EXISTS orchestration_artifacts(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES orchestration_runs(id) ON DELETE CASCADE,
                step_id TEXT NOT NULL REFERENCES orchestration_steps(id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES orchestration_attempts(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                baseline_ref TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                verified INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orch_artifacts_run ON orchestration_artifacts(run_id, created ASC);

            CREATE TABLE IF NOT EXISTS orchestration_dispatches(
                attempt_id TEXT PRIMARY KEY REFERENCES orchestration_attempts(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES orchestration_runs(id) ON DELETE CASCADE,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                generation INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orch_dispatches_status ON orchestration_dispatches(status, created ASC);
        ''')
        _ensure_columns(db, 'sessions', {
            'execution_target': "TEXT NOT NULL DEFAULT 'local'",
            'cloud_environment': "TEXT NOT NULL DEFAULT ''",
            'model': 'TEXT',
            'effort': 'TEXT',
            'mode': "TEXT NOT NULL DEFAULT 'standard'",
            'fast': 'INTEGER NOT NULL DEFAULT 0',
            'pinned': 'INTEGER NOT NULL DEFAULT 0',
            'cwd': 'TEXT',
            'originator': 'TEXT',
            'thread_kind': "TEXT NOT NULL DEFAULT 'user'",
            'git_branch': 'TEXT',
            'tokens_used': 'INTEGER NOT NULL DEFAULT 0',
            'rollout_path': 'TEXT',
            'codex_synced_ordinal': 'INTEGER NOT NULL DEFAULT -1',
            'codex_rollout_offset': 'INTEGER NOT NULL DEFAULT 0',
            'codex_name': 'TEXT',
            'codex_pinned': 'INTEGER',
            'antigravity_synced_step': 'INTEGER NOT NULL DEFAULT -1',
            'antigravity_transcript_offset': 'INTEGER NOT NULL DEFAULT 0',
            'antigravity_not_idle': 'INTEGER NOT NULL DEFAULT 0',
            'use_worktree': 'INTEGER NOT NULL DEFAULT 0',
            'worktree_path': 'TEXT',
            'native_codex_id': 'TEXT',
            'native_antigravity_id': 'TEXT',
            'native_agent_ids': "TEXT NOT NULL DEFAULT '{}'",
            'last_engine': 'TEXT',
            'external_stopped_at': 'REAL NOT NULL DEFAULT 0',
            'external_stopped_marker': "TEXT NOT NULL DEFAULT ''",
            'pruned_through_seq': 'INTEGER NOT NULL DEFAULT 0',
            'queue_paused': 'INTEGER NOT NULL DEFAULT 0',
            'orchestration': "TEXT NOT NULL DEFAULT 'off'",
            'orch_role': 'TEXT',
            'active_orch_run_id': 'TEXT',
        })
        _ensure_columns(db, 'projects', {
            'is_scratch': 'INTEGER NOT NULL DEFAULT 0',
        })
        _ensure_columns(db, 'jobs', {
            'execution_target': "TEXT NOT NULL DEFAULT 'local'",
            'cloud_environment': "TEXT NOT NULL DEFAULT ''",
            'cloud_task_id': 'TEXT',
            'checkpoint_ref': "TEXT NOT NULL DEFAULT ''",
            'checkpoint_after_ref': "TEXT NOT NULL DEFAULT ''",
            'reverted': 'INTEGER NOT NULL DEFAULT 0',
            'model': 'TEXT',
            'effort': 'TEXT',
            'mode': 'TEXT',
            'fast': 'INTEGER NOT NULL DEFAULT 0',
            'images': "TEXT NOT NULL DEFAULT '[]'",
            'engine': 'TEXT',
            'orch_run_id': 'TEXT',
            'orch_step_id': 'TEXT',
            'orch_attempt_id': 'TEXT',
            'orch_generation': 'INTEGER',
        })
        db.execute('''
            UPDATE sessions
            SET native_codex_id = COALESCE(native_codex_id, CASE WHEN engine = 'codex' AND (native_antigravity_id IS NULL OR native_id != native_antigravity_id) THEN native_id END),
                native_antigravity_id = COALESCE(native_antigravity_id, CASE WHEN engine = 'antigravity' AND (native_codex_id IS NULL OR native_id != native_codex_id) THEN native_id END),
                last_engine = COALESCE(last_engine, engine)
        ''')
        db.execute('''
            UPDATE sessions
            SET native_codex_id = NULL
            WHERE native_codex_id IS NOT NULL AND native_codex_id = native_antigravity_id
        ''')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_native_session ON sessions(engine, native_id) WHERE native_id IS NOT NULL')
        db.execute('CREATE INDEX IF NOT EXISTS idx_sessions_native_codex ON sessions(native_codex_id) WHERE native_codex_id IS NOT NULL')
        db.execute('CREATE INDEX IF NOT EXISTS idx_sessions_native_agy ON sessions(native_antigravity_id) WHERE native_antigravity_id IS NOT NULL')
    os.chmod(DATA / 'state.sqlite3', 0o600)
    allowed_roots = [Path(r).resolve() for r in config().get('allowed_roots', []) if r]
    if any(ROOT.parent.resolve().is_relative_to(r) or r == ROOT.parent.resolve() for r in allowed_roots):
        for p in ROOT.parent.iterdir():
            if p.is_dir() and not p.name.startswith('.'):
                execute('INSERT OR IGNORE INTO projects(id,name,path) VALUES(?,?,?)', (secrets.token_hex(8), p.name, str(p.resolve())))


def get_codex_native_id(session_row: dict | None) -> str | None:
    if not session_row:
        return None
    cid = session_row.get('native_codex_id')
    aid = session_row.get('native_antigravity_id')
    if cid and cid != aid:
        return cid
    nid = session_row.get('native_id')
    if (
        nid
        and nid != aid
        and session_row.get('engine') == 'codex'
        and not str(session_row.get('id', '')).startswith('agy_')
    ):
        return nid
    return None


def get_antigravity_native_id(session_row: dict | None) -> str | None:
    if not session_row:
        return None
    aid = session_row.get('native_antigravity_id')
    cid = session_row.get('native_codex_id')
    if aid and aid != cid:
        return aid
    nid = session_row.get('native_id')
    if nid and nid != cid and session_row.get('engine') == 'antigravity':
        return nid
    return None


def session_workdir(session_row, project_row):
    allowed = [Path(r).resolve() for r in config().get('allowed_roots', [])]
    if (session_row or {}).get('use_worktree') and (session_row or {}).get('worktree_path'):
        try:
            wt = Path(session_row['worktree_path']).resolve()
            if wt.is_dir() and any(wt.is_relative_to(root) for root in allowed):
                return str(wt)
        except OSError:
            pass
    raw_cwd = (session_row or {}).get('cwd')
    if raw_cwd:
        with contextlib.suppress(OSError):
            resolved = Path(raw_cwd).resolve()
            if resolved.is_dir() and any(resolved.is_relative_to(root) for root in allowed):
                return str(resolved)
    return project_row['path']


def event(session, job, kind, payload):
    return execute('INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                   (session, job, kind, json.dumps(payload, ensure_ascii=False), time.time()))


def alive(pid, expected=None):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        if expected:
            proc_cmdline = Path(f'/proc/{pid}/cmdline')
            if proc_cmdline.is_file():
                raw = proc_cmdline.read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='replace')
                return expected in raw
            ps_bin = '/bin/ps' if Path('/bin/ps').is_file() else 'ps'
            command = subprocess.run([ps_bin, '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=2).stdout
            return expected in command
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def reconcile(min_interval: float = 0.0):
    global _last_reconcile_ts
    now = time.time()
    if min_interval > 0 and (now - _last_reconcile_ts) < min_interval:
        return
    with _reconcile_lock:
        if min_interval > 0 and (time.time() - _last_reconcile_ts) < min_interval:
            return
        _last_reconcile_ts = time.time()
        # Only reconcile registered processes; never replay an ambiguous AI request.
        for j in query("SELECT * FROM jobs WHERE status IN ('running','waiting')"):
            if not alive(j['pid'], '-m workstation.worker ' + j['id']):
                execute("UPDATE jobs SET status='interrupted',ended=?,error=? WHERE id=? AND status IN ('running','waiting')",
                        (time.time(), '执行进程已退出。请检查已有修改后继续原会话。', j['id']))
        for t in query("SELECT * FROM terminals WHERE status IN ('starting','running')"):
            if not alive(t['pid'], '-m workstation.terminal ' + t['id']) and time.time() - t['created'] > 15:
                execute("UPDATE terminals SET status='exited' WHERE id=?", (t['id'],))
        now = time.time()
        execute("DELETE FROM logins WHERE expires < ?", (now,))
        execute("DELETE FROM terminal_output WHERE terminal_id IN (SELECT id FROM terminals WHERE status != 'running' AND created < ?)", (now - 7 * 86400,))
