import asyncio
import base64
import collections
import contextlib
from contextlib import asynccontextmanager, closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import store as db
from . import agents
from . import cloud_execution
from . import cluster_sync
from . import engines
from . import codex_sync
from . import antigravity_hub
from . import antigravity_sync
from . import quotas
from . import webpush
from . import memories
from . import worktree
from . import feishu_relay
from . import design_guard
from . import retention
from . import input_handoff
from . import quota_wait
from . import review
from . import account_pool
from . import fork
from . import model_router
from . import webhooks

COOKIE = 'walking_session'
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$')
UPLOAD_NAME_RE = re.compile(r'^[a-f0-9]{16,32}\.(png|jpg|webp|gif)$')
DATA_URL_RE = re.compile(r'^data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)$')
SKIP_DIRS = {'node_modules', '__pycache__', 'dist', 'build', 'target', 'venv'}

EffortType = Literal['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
ModeType = Literal['standard', 'plan', 'auto', 'accept-edits', 'full']
EngineType = str


def spawn(module, ident):
    logs = db.DATA / 'logs'
    logs.mkdir(exist_ok=True, mode=0o700)
    with (logs / 'workers.log').open('ab') as log:
        proc = subprocess.Popen([sys.executable, '-m', module, ident], cwd=db.ROOT,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True, env=os.environ.copy())
    return proc.pid


_last_auto_maintenance_ts: float = 0.0


async def _background_retention_loop():
    """Lightweight background retention loop that runs daily to compact streaming deltas and prune expired logins."""
    global _last_auto_maintenance_ts
    while True:
        try:
            await asyncio.sleep(3600)  # Check hourly
            now = time.time()
            if now - _last_auto_maintenance_ts >= 86400:  # Run once every 24h
                # Only run when no jobs are active
                active_jobs = db.one("SELECT count(*) as c FROM jobs WHERE status IN ('queued', 'running', 'waiting')")
                if not active_jobs or active_jobs['c'] == 0:
                    _last_auto_maintenance_ts = now
                    await asyncio.to_thread(
                        retention.run_lifecycle_maintenance,
                        {
                            'keep_days': 14,
                            'compact_events': True,
                            'prune_sessions': False,
                            'vacuum': False,
                            'backup_before': False,
                            'dry_run': False,
                        },
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            pass


def _resume_quota_wait_job(wait_row: dict) -> bool:
    s = db.one('SELECT * FROM sessions WHERE id=?', (wait_row['session_id'],))
    if not s:
        return False
    resume_prompt = (
        '请继续完成刚才因额度限制暂停的任务。'
        if wait_row.get('has_output') else
        (wait_row.get('prompt') or '请继续执行任务。')
    )
    job_id = secrets.token_hex(12)
    req_id = f"qw-{wait_row['id']}-{secrets.token_hex(4)}"
    now = time.time()
    try:
        with db.transaction() as conn:
            conn.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,model,effort,mode,fast,images) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (
                    job_id,
                    s['id'],
                    req_id,
                    resume_prompt,
                    'queued',
                    now,
                    wait_row.get('model') or s.get('model'),
                    s.get('effort'),
                    s.get('mode') or 'standard',
                    int(bool(s.get('fast'))),
                    '[]',
                ),
            )
            conn.execute(
                'INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                (
                    s['id'],
                    job_id,
                    'notice',
                    json.dumps({'text': '✅ 用量已恢复，已自动继续执行本任务。'}, ensure_ascii=False),
                    now,
                ),
            )
        spawn('workstation.worker', job_id)
        return True
    except Exception:
        return False


async def _background_quota_wait_loop():
    """Poll due quota-wait entries every 30s and auto-resume eligible sessions."""
    while True:
        try:
            await asyncio.sleep(30)
            await asyncio.to_thread(quota_wait.process_due_quota_waits, _resume_quota_wait_job)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app):
    db.init()
    db.reconcile()
    await asyncio.to_thread(codex_sync.sync_projects_and_threads, True)
    await asyncio.to_thread(antigravity_sync.sync_antigravity_projects_and_sessions, True)
    awake = await asyncio.create_subprocess_exec('/usr/bin/caffeinate', '-i', '-w', str(os.getpid())) if sys.platform == 'darwin' else None
    for job in db.query("SELECT id FROM jobs WHERE status='queued'"):
        spawn('workstation.worker', job['id'])
    maint_task = asyncio.create_task(_background_retention_loop())
    quota_task = asyncio.create_task(_background_quota_wait_loop())
    yield
    # Do not terminate detached workers when the HTTP server shuts down.
    for task in (maint_task, quota_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if awake:
        awake.terminate()
        await awake.wait()


_bg_tasks: set = set()
app = FastAPI(title='Rove', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
db.init()
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=db.config()['allowed_hosts'])


def logged_in(token):
    if not token:
        return False
    return bool(db.one('SELECT 1 FROM logins WHERE token_hash=? AND expires>?',
                       (hashlib.sha256(token.encode()).hexdigest(), time.time())))


@app.middleware('http')
async def security(request: Request, call_next):
    path = request.url.path
    if path.startswith('/api/'):
        is_watch_public = (path.startswith('/api/watch/') or path.startswith('/watch/')) and path != '/api/watch/config'
        is_eink_public = path.startswith('/api/eink/') or path.startswith('/eink')
        is_feishu_webhook = path == '/api/feishu/webhook'
        is_cluster_token_auth = (
            path in ('/api/cluster/ping', '/api/cluster/snapshot/export', '/api/cluster/snapshot/import')
            and cluster_sync.verify_cluster_token(request.headers.get('x-cluster-token'))
        )
        is_github_webhook = (path == '/api/webhooks/github' and request.method == 'POST')
        if not is_watch_public and not is_eink_public and not is_feishu_webhook and not is_cluster_token_auth and not is_github_webhook:
            if request.method not in ('GET', 'HEAD'):
                origin = request.headers.get('origin')
                if (origin and origin not in db.config()['origins']) or request.headers.get('x-workstation') != '1':
                    return JSONResponse({'detail': '请求来源无效'}, status_code=403)
            if path != '/api/login' and not logged_in(request.cookies.get(COOKIE)):
                return JSONResponse({'detail': '请登录工作台'}, status_code=401)
    response = await call_next(request)
    websocket_origins = ' '.join(o.replace('https://', 'wss://').replace('http://', 'ws://') for o in db.config()['origins'])
    if (path.startswith('/assets/') or path.startswith('/watch/assets/')) and response.status_code == 200:
        cache_control = 'public, max-age=31536000, immutable'
        rel_asset = path.removeprefix('/watch').lstrip('/')
        asset_path = db.ROOT / 'frontend/dist' / rel_asset
        if asset_path.is_file():
            response.headers['X-Rove-Asset-Bytes'] = str(asset_path.stat().st_size)
    elif path in ('/sw.js', '/manifest.webmanifest', '/manifest.json'):
        cache_control = 'no-cache'
    elif path in (
        '/favicon.svg', '/favicon.ico', '/rove-favicon.svg',
        '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png', '/rove-apple-touch.png',
        '/pwa-192.png', '/rove-192.png',
        '/pwa-512.png', '/rove-512.png',
        '/pwa-maskable-512.png', '/rove-maskable-512.png',
    ):
        cache_control = 'no-cache, no-store, must-revalidate'
    else:
        cache_control = 'no-store'
    script_csp = "script-src 'self' 'unsafe-inline'"
    response.headers.update({
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'no-referrer',
        'Cache-Control': cache_control,
        'Content-Security-Policy': (
            f"default-src 'self'; {script_csp}; worker-src 'self'; manifest-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self' " + websocket_origins + "; "
            "img-src 'self' https: data: blob:; media-src 'self' data: blob:; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        ),
    })
    return response


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=512)


attempts = {}
password_checks = 0
MAX_PASSWORD_CHECKS = 4


@app.post('/api/login')
async def login(body: Login, request: Request, response: Response):
    global password_checks
    # X-Forwarded-For is honored by Uvicorn only from the configured trusted proxy.
    peer = request.client.host
    now = time.time()
    if len(attempts) > 512:
        stale_peers = [k for k, ts_list in attempts.items() if not ts_list or now - ts_list[-1] >= 600]
        for k in stale_peers:
            attempts.pop(k, None)
    recent = [t for t in attempts.get(peer, []) if now - t < 600]
    if len(recent) >= 12:
        raise HTTPException(429, '尝试次数过多，请十分钟后重试')
    if password_checks >= MAX_PASSWORD_CHECKS:
        raise HTTPException(429, '登录请求繁忙，请稍后重试')
    attempts[peer] = recent + [now]
    auth = json.loads((db.DATA / 'auth.json').read_text())
    password_checks += 1
    check = asyncio.create_task(asyncio.to_thread(db.password_hash, body.password, auth['salt']))
    def release_check(_):
        global password_checks
        password_checks -= 1
    check.add_done_callback(release_check)
    value = await asyncio.shield(check)
    if not hmac.compare_digest(value, auth['hash']):
        raise HTTPException(401, '访问密码不正确')
    remaining = [stamp for stamp in attempts.get(peer, []) if stamp > now]
    if remaining:
        attempts[peer] = remaining
    else:
        attempts.pop(peer, None)
    token = secrets.token_urlsafe(32)
    db.execute('DELETE FROM logins WHERE expires<?', (now,))
    db.execute('INSERT INTO logins VALUES(?,?)', (hashlib.sha256(token.encode()).hexdigest(), now + 7 * 86400))
    response.set_cookie(COOKIE, token, max_age=7 * 86400, httponly=True, secure=request.url.scheme == 'https', samesite='lax', path='/')
    return {'ok': True}


@app.post('/api/logout')
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        db.execute('DELETE FROM logins WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


def project(ident):
    value = db.one('SELECT * FROM projects WHERE id=?', (ident,))
    if not value:
        raise HTTPException(404, '项目不存在')
    resolved = Path(value['path']).resolve()
    if not any(resolved.is_relative_to(Path(root).resolve()) for root in db.config()['allowed_roots']):
        raise HTTPException(403, '项目不在允许的目录内')
    if not resolved.is_dir():
        raise HTTPException(404, '项目目录不可用')
    return value


def session(ident):
    value = db.one('SELECT * FROM sessions WHERE id=?', (ident,))
    if not value:
        raise HTTPException(404, '会话不存在')
    project(value['project_id'])
    return value


def clean_model(model: str | None) -> str | None:
    if model is None or model == '':
        return None
    value = model.strip()
    if not MODEL_RE.fullmatch(value):
        raise HTTPException(400, '模型标识格式无效')
    return value


_session_sync_throttles: dict[str, float] = {}
_session_sync_tasks: dict[str, asyncio.Task[int]] = {}


async def _sync_session_events(ident: str) -> int:
    sess = db.one('SELECT * FROM sessions WHERE id=?', (ident,))
    if not sess:
        return 0
    now = time.monotonic()
    if len(_session_sync_throttles) > 200:
        cutoff = now - 300.0
        stale = [k for k, ts in _session_sync_throttles.items() if ts < cutoff]
        for k in stale:
            _session_sync_throttles.pop(k, None)

    # Coalesce high-frequency polling calls within 1.2 seconds
    if (now - _session_sync_throttles.get(ident, 0.0)) < 1.2:
        existing_task = _session_sync_tasks.get(ident)
        if existing_task and not existing_task.done():
            return await existing_task
        return 0

    async def _do_sync() -> int:
        added = 0
        if db.get_antigravity_native_id(sess):
            added += await asyncio.to_thread(antigravity_sync.sync_antigravity_session_events, ident)
        if db.get_codex_native_id(sess):
            added += await asyncio.to_thread(codex_sync.sync_session_events, ident)
        return added

    task = asyncio.create_task(_do_sync())
    _session_sync_tasks[ident] = task
    _session_sync_throttles[ident] = now
    try:
        return await task
    finally:
        if _session_sync_tasks.get(ident) is task:
            _session_sync_tasks.pop(ident, None)


@app.get('/api/state')
async def state(sync: bool = True):
    await asyncio.to_thread(db.reconcile, 2.5)
    if sync:
        await asyncio.to_thread(codex_sync.sync_projects_and_threads)
        await asyncio.to_thread(antigravity_sync.sync_antigravity_projects_and_sessions)
    cfg = db.config()
    allowed_set = set()
    for r in cfg.get('allowed_roots', []):
        try:
            allowed_set.add(str(Path(r).resolve()))
        except OSError:
            pass
    projects_list = db.query('SELECT * FROM projects')
    counts = {r['project_id']: r['c'] for r in db.query('SELECT project_id, count(*) as c FROM project_memories GROUP BY project_id')}
    for p in projects_list:
        p['memories_count'] = counts.get(p['id'], 0)
        try:
            p['is_workspace'] = str(Path(p['path']).resolve()) in allowed_set
        except OSError:
            p['is_workspace'] = False
    projects_list.sort(key=lambda p: (not p['is_workspace'], p['name'].lower()))

    sessions_list = db.query("SELECT * FROM sessions WHERE COALESCE(thread_kind, 'user') != 'review' ORDER BY pinned DESC, updated DESC")
    jobs_list = db.query('SELECT id,session_id,status,started,ended,error,model,effort,mode,fast,engine,execution_target,cloud_task_id FROM jobs ORDER BY rowid DESC LIMIT 200')
    active_local = {j['session_id'] for j in jobs_list if j['status'] in ('queued', 'running', 'waiting')}
    live_map_codex = await asyncio.to_thread(codex_sync.live_thread_states, sessions_list, active_local)
    live_map_agy = await asyncio.to_thread(antigravity_sync.live_antigravity_states, sessions_list, active_local)
    for s in sessions_list:
        sid = s['id']
        stc = live_map_codex.get(sid)
        sta = live_map_agy.get(sid)
        s['external_active'] = bool((stc and stc.get('external_active')) or (sta and sta.get('external_active')))
        s['writer_locked'] = bool((stc and stc.get('writer_locked')) or (sta and sta.get('writer_locked')))

    home = codex_sync.codex_home()
    agy_home = antigravity_sync.antigravity_home()
    all_agents = agents.list_all_agents()
    engines_map = {
        k: {'installed': Path(cfg[k + '_bin']).is_file(), **engines.state(k)}
        for k in ('codex', 'antigravity')
    }
    for ag in all_agents:
        if not ag.get('builtin'):
            engines_map[ag['id']] = {
                'installed': bool(ag.get('installed')),
                **(ag.get('check') or {}),
            }
    return {
        'host': cluster_sync.get_cluster_config().get('node_name') or 'Mac mini',
        'projects': projects_list,
        'sessions': sessions_list,
        'jobs': jobs_list,
        'engines': engines_map,
        'agents': all_agents,
        'catalog': engines.catalog(),
        'codex_sync': {
            'enabled': bool(home),
            'codex_home': str(home) if home else None,
            'external_active_count': sum(1 for s in sessions_list if s.get('engine') == 'codex' and s['external_active']),
        },
        'antigravity_sync': {
            'enabled': bool(agy_home),
            'antigravity_home': str(agy_home) if agy_home else None,
            'external_active_count': sum(1 for s in sessions_list if s.get('engine') == 'antigravity' and s['external_active']),
        },
        'memory_categories': memories.CATEGORIES,
        'web_push': webpush.get_push_status(),
        'feishu': feishu_relay.get_feishu_status(),
    }


class SyncBody(BaseModel):
    restore_dismissed: bool = False
    session_id: str | None = None
    engine: str | None = None


@app.post('/api/sync')
async def manual_sync(body: SyncBody | None = None):
    req = body or SyncBody()
    res = {}
    if req.engine in (None, 'codex'):
        codex_res = await asyncio.to_thread(codex_sync.sync_projects_and_threads, True, req.restore_dismissed)
        res['codex'] = codex_res
    if req.engine in (None, 'antigravity'):
        agy_res = await asyncio.to_thread(antigravity_sync.sync_antigravity_projects_and_sessions, True, req.restore_dismissed)
        res['antigravity'] = agy_res
    events_added = 0
    if req.session_id:
        events_added = await _sync_session_events(req.session_id)
    return {'ok': True, 'events_added': events_added, **res}


@app.post('/api/codex/sync')
async def manual_codex_sync(body: SyncBody | None = None):
    req = body or SyncBody()
    result = await asyncio.to_thread(codex_sync.sync_projects_and_threads, True, req.restore_dismissed)
    events_added = 0
    if req.session_id:
        events_added = await asyncio.to_thread(codex_sync.sync_session_events, req.session_id)
    return {'ok': True, 'events_added': events_added, **result}


@app.post('/api/antigravity/sync')
async def manual_antigravity_sync(body: SyncBody | None = None):
    req = body or SyncBody()
    result = await asyncio.to_thread(antigravity_sync.sync_antigravity_projects_and_sessions, True, req.restore_dismissed)
    events_added = 0
    if req.session_id:
        events_added = await asyncio.to_thread(antigravity_sync.sync_antigravity_session_events, req.session_id)
    return {'ok': True, 'events_added': events_added, **result}


@app.get('/api/quotas')
async def fetch_quotas(force: bool = False):
    return await quotas.get_all_quotas(force=force)


@app.post('/api/quotas/refresh')
async def refresh_quotas():
    return await quotas.get_all_quotas(force=True)


engine_check_lock = asyncio.Lock()
model_refresh_lock = asyncio.Lock()


@app.post('/api/engines/antigravity/check')
async def check_antigravity():
    if engine_check_lock.locked():
        raise HTTPException(409, '连接检查正在进行，请稍候')
    async with engine_check_lock:
        try:
            return await engines.check_antigravity()
        except OSError as exc:
            raise HTTPException(503, '无法启动 Antigravity 连接检查') from exc


@app.post('/api/models/refresh')
async def refresh_models():
    if model_refresh_lock.locked():
        raise HTTPException(409, '模型列表正在同步，请稍候')
    async with model_refresh_lock:
        return await engines.refresh_models()


class NewProject(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


@app.post('/api/projects')
async def add_project(body: NewProject):
    path = Path(body.path).expanduser().resolve()
    if not path.is_dir() or any(p.startswith('.') for p in path.parts):
        raise HTTPException(400, '请选择可用的项目目录')
    if not any(path.is_relative_to(Path(root).resolve()) for root in db.config()['allowed_roots']):
        raise HTTPException(403, '目录不在本机配置的允许范围内')
    ident = secrets.token_hex(12)
    db.execute('INSERT OR IGNORE INTO projects VALUES(?,?,?)', (ident, path.name, str(path)))
    return db.one('SELECT * FROM projects WHERE path=?', (str(path),))


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    category: str = Field(default='convention', max_length=40)
    pinned: bool = False


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=40)
    pinned: bool | None = None


@app.get('/api/projects/{ident}/memories')
async def get_project_memories(ident: str):
    project(ident)
    return {
        'project_id': ident,
        'memories': memories.list_memories(ident),
        'categories': memories.CATEGORIES,
    }


@app.post('/api/projects/{ident}/memories')
async def create_project_memory(ident: str, body: MemoryCreate):
    project(ident)
    try:
        item = memories.add_memory(
            project_id=ident,
            content=body.content,
            category=body.category,
            pinned=body.pinned,
        )
        return item
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch('/api/memories/{ident}')
async def update_project_memory(ident: str, body: MemoryPatch):
    mem = memories.get_memory(ident)
    if not mem:
        raise HTTPException(404, '记忆条目不存在')
    try:
        updated = memories.update_memory(
            memory_id=ident,
            content=body.content,
            category=body.category,
            pinned=body.pinned,
        )
        return updated
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete('/api/memories/{ident}')
async def delete_project_memory(ident: str):
    mem = memories.get_memory(ident)
    if not mem:
        raise HTTPException(404, '记忆条目不存在')
    memories.delete_memory(ident)
    return {'ok': True, 'id': ident}


class MemoryConsolidateBody(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1, max_length=2000)
    category: str = Field(default='convention', max_length=40)
    why: str = Field(default='', max_length=500)
    how_to_apply: str = Field(default='', max_length=500)
    pinned: bool = False


@app.get('/api/projects/{ident}/memories/search')
async def search_project_memories(ident: str, q: str = '', limit: int = 8):
    project(ident)
    return {
        'project_id': ident,
        'query': q,
        'memories': memories.search_memories(ident, q, limit=max(1, min(limit, 50))),
    }


@app.post('/api/projects/{ident}/memories/consolidate')
async def consolidate_project_memories(ident: str, body: MemoryConsolidateBody):
    project(ident)
    try:
        return memories.consolidate_memories(
            project_id=ident,
            source_ids=body.source_ids,
            target_content=body.content,
            category=body.category,
            why=body.why,
            how_to_apply=body.how_to_apply,
            pinned=body.pinned,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post('/api/sessions/{ident}/extract-memory')
async def extract_session_memory(ident: str):
    s = session(ident)
    last_job = db.one('SELECT * FROM jobs WHERE session_id=? ORDER BY started DESC LIMIT 1', (ident,))
    if not last_job:
        raise HTTPException(404, '该会话尚无已执行的任务')
    p = project(s['project_id'])
    saved = memories.auto_record_from_job(s, last_job, p)
    return {'extracted': len(saved), 'items': saved}


class WorktreeMergeBody(BaseModel):
    message: str | None = Field(default=None, max_length=240)


@app.get('/api/projects/{ident}/worktrees')
async def get_project_worktrees(ident: str):
    p = project(ident)
    is_git = await asyncio.to_thread(worktree.is_git_repository, p['path'])
    trees = await asyncio.to_thread(worktree.list_project_worktrees, p) if is_git else []
    has_head = await asyncio.to_thread(worktree.has_valid_head, p['path']) if is_git else False
    return {
        'project_id': ident,
        'is_git': is_git,
        'has_head': has_head,
        'worktrees': trees,
    }


@app.get('/api/sessions/{ident}/worktree')
async def get_session_worktree_status(ident: str):
    s = session(ident)
    p = project(s['project_id'])
    is_git = await asyncio.to_thread(worktree.is_git_repository, p['path'])
    has_head = await asyncio.to_thread(worktree.has_valid_head, p['path']) if is_git else False
    wt = await asyncio.to_thread(worktree.get_session_worktree, p, s)
    return {
        'session_id': ident,
        'is_git': is_git,
        'has_head': has_head,
        'use_worktree': bool(s.get('use_worktree')),
        'worktree': wt,
    }


@app.post('/api/sessions/{ident}/worktree/enable')
async def enable_session_worktree(ident: str):
    s = session(ident)
    p = project(s['project_id'])
    try:
        wt = await asyncio.to_thread(worktree.create_session_worktree, p, s)
        return wt
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post('/api/sessions/{ident}/worktree/merge')
async def merge_session_worktree(ident: str, body: WorktreeMergeBody | None = None):
    s = session(ident)
    p = project(s['project_id'])
    msg = body.message if body else None
    try:
        result = await asyncio.to_thread(worktree.merge_session_worktree, p, s, msg)
        return result
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.delete('/api/sessions/{ident}/worktree')
async def delete_session_worktree(ident: str, force: bool = True):
    s = session(ident)
    p = project(s['project_id'])
    await asyncio.to_thread(worktree.remove_session_worktree, p, s, force=force)
    return {'ok': True, 'session_id': ident}


@app.get('/api/sessions/{ident}/worktree/diff')
async def get_session_worktree_diff(ident: str):
    s = session(ident)
    p = project(s['project_id'])
    diff_text = await asyncio.to_thread(worktree.get_worktree_diff, p, s)
    return {'diff': diff_text}


class NewSession(BaseModel):
    project_id: str
    engine: str = Field(default='codex', min_length=2, max_length=40)
    title: str = Field(default='新会话', min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    effort: EffortType | None = None
    mode: ModeType = 'standard'
    fast: bool = False


@app.post('/api/sessions')
async def new_session(body: NewSession):
    project(body.project_id)
    if not agents.is_valid_engine(body.engine):
        raise HTTPException(400, f'未知或未启用的 Agent 引擎：{body.engine}')
    model = clean_model(body.model)
    ident, now = secrets.token_hex(12), time.time()
    db.execute(
        'INSERT INTO sessions(id,project_id,engine,title,native_id,created,updated,model,effort,mode,fast,pinned) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,0)',
        (ident, body.project_id, body.engine, body.title.strip() or '新会话', now, now, model, body.effort, body.mode, int(body.fast)),
    )
    return session(ident)


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None
    engine: EngineType | None = None
    model: str | None = Field(default=None, max_length=120)
    effort: EffortType | None = None
    mode: ModeType | None = None
    fast: bool | None = None
    account_id: str | None = Field(default=None, max_length=64)
    generation: int | None = None
    defer_if_busy: bool = False


@app.patch('/api/sessions/{ident}')
@app.post('/api/sessions/{ident}/update')
async def update_session(ident: str, body: SessionPatch):
    current = session(ident)
    fields = body.model_fields_set
    if 'generation' in fields and body.generation is not None:
        cur_gen = int(current.get('runtime_generation') or 1)
        if body.generation != cur_gen:
            raise HTTPException(409, f'会话运行时版本已过期 (expected {cur_gen}, got {body.generation})')
    updates, params = [], []
    renamed_to = None
    active_job = db.one("SELECT id FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting') LIMIT 1", (current['id'],))
    runtime_keys = {'engine', 'model', 'effort', 'mode', 'fast'}
    if body.defer_if_busy and active_job and (fields & runtime_keys):
        try:
            pending_rt = json.loads(current.get('pending_runtime_json') or '{}')
        except Exception:
            pending_rt = {}
        if not isinstance(pending_rt, dict):
            pending_rt = {}
        for k in ('engine', 'model', 'effort', 'mode'):
            if k in fields and getattr(body, k) is not None:
                pending_rt[k] = getattr(body, k)
        if 'fast' in fields and body.fast is not None:
            pending_rt['fast'] = int(body.fast)
        updates.extend(['pending_runtime_json=?', 'runtime_generation=COALESCE(runtime_generation,1)+1'])
        params.extend([json.dumps(pending_rt, ensure_ascii=False)])
        fields = fields - runtime_keys
    if 'account_id' in fields:
        updates.append('account_id=?')
        params.append(body.account_id or None)
    if 'title' in fields and body.title is not None:
        cleaned = body.title.strip()
        if not cleaned:
            raise HTTPException(400, '会话标题不能为空')
        updates.append('title=?')
        params.append(cleaned)
        renamed_to = cleaned
        if db.get_codex_native_id(current):
            updates.append('codex_name=?')
            params.append(cleaned)
    if 'pinned' in fields and body.pinned is not None:
        updates.append('pinned=?')
        params.append(int(body.pinned))
    if 'engine' in fields and body.engine and agents.is_valid_engine(body.engine):
        orig_codex_id = db.get_codex_native_id(current)
        orig_agy_id = db.get_antigravity_native_id(current)
        target_nid = agents.get_session_native_agent_id(current, body.engine)
        updates.extend(['engine=?', 'native_id=?', 'native_codex_id=?', 'native_antigravity_id=?'])
        params.extend([body.engine, target_nid, orig_codex_id, orig_agy_id])
    next_engine = body.engine if ('engine' in fields and body.engine and agents.is_valid_engine(body.engine)) else current['engine']
    if 'model' in fields or 'engine' in fields:
        quota_wait.cancel_quota_wait(current['id'], reason='model_or_engine_switched')
        wanted_model = clean_model(body.model) if 'model' in fields else current.get('model')
        wanted_effort = body.effort if 'effort' in fields else current.get('effort')
        resolved_model, resolved_effort = engines.resolve_model(next_engine, wanted_model, wanted_effort)
        updates.append('model=?')
        params.append(resolved_model)
        if 'effort' in fields or resolved_effort != current.get('effort'):
            updates.append('effort=?')
            params.append(resolved_effort)
    elif 'effort' in fields:
        updates.append('effort=?')
        params.append(body.effort)
    if 'mode' in fields and body.mode is not None:
        updates.append('mode=?')
        params.append(body.mode)
    if 'fast' in fields and body.fast is not None:
        updates.append('fast=?')
        params.append(int(body.fast))
    if updates:
        updates.append('updated=?')
        params.append(time.time())
        params.append(current['id'])
        db.execute(f"UPDATE sessions SET {','.join(updates)} WHERE id=?", tuple(params))
    codex_nid = db.get_codex_native_id(current)
    if renamed_to and codex_nid:
        task = asyncio.create_task(codex_sync.push_codex_thread_name(codex_nid, renamed_to))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    return session(ident)


@app.delete('/api/sessions/{ident}')
async def delete_session(ident: str):
    current = session(ident)
    await asyncio.to_thread(db.reconcile, 2.0)
    with db.transaction() as conn:
        live_job = conn.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting')", (ident,)).fetchone()
        live_term = conn.execute("SELECT 1 FROM terminals WHERE session_id=? AND status IN ('starting','running')", (ident,)).fetchone()
        if live_job or live_term:
            raise HTTPException(409, '会话仍有运行中的任务或终端，请先停止')
        conn.execute('DELETE FROM terminal_output WHERE terminal_id IN (SELECT id FROM terminals WHERE session_id=?)', (ident,))
        conn.execute('DELETE FROM terminal_input WHERE terminal_id IN (SELECT id FROM terminals WHERE session_id=?)', (ident,))
        conn.execute('DELETE FROM terminals WHERE session_id=?', (ident,))
        conn.execute('DELETE FROM approvals WHERE job_id IN (SELECT id FROM jobs WHERE session_id=?)', (ident,))
        conn.execute('DELETE FROM synced_codex_items WHERE session_id=?', (ident,))
        conn.execute('DELETE FROM events WHERE session_id=?', (ident,))
        conn.execute('DELETE FROM jobs WHERE session_id=?', (ident,))
        conn.execute('DELETE FROM sessions WHERE id=?', (ident,))
        # A relay session may be bound to both engines: dismiss every native thread so sync cannot resurrect it.
        codex_nid = db.get_codex_native_id(current)
        agy_nid = db.get_antigravity_native_id(current)
        if codex_nid:
            conn.execute(
                'INSERT OR REPLACE INTO dismissed_codex_threads(native_id,dismissed_at) VALUES(?,?)',
                (codex_nid, time.time()),
            )
        if agy_nid:
            conn.execute(
                'INSERT OR REPLACE INTO dismissed_antigravity_threads(native_id,dismissed_at) VALUES(?,?)',
                (agy_nid, time.time()),
            )
    upload_dir = (db.DATA / 'uploads' / ident).resolve()
    if upload_dir.is_relative_to((db.DATA / 'uploads').resolve()):
        shutil.rmtree(upload_dir, ignore_errors=True)
    return {'ok': True}


def _save_images(session_id: str, items: list[str]) -> list[str]:
    if not items:
        return []
    folder = db.DATA / 'uploads' / session_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = []
    for raw_url in items:
        match = DATA_URL_RE.match(raw_url.strip())
        if not match:
            raise HTTPException(400, '仅支持 PNG、JPEG、WebP 或 GIF 图片')
        fmt, encoded = match.group(1), re.sub(r'\s+', '', match.group(2))
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise HTTPException(400, '图片编码无效') from exc
        if not data or len(data) > 4 * 1024 * 1024:
            raise HTTPException(413, '单张图片不能超过 4 MB')
        valid_magic = (
            (fmt == 'png' and data.startswith(b'\x89PNG\r\n\x1a\n'))
            or (fmt in ('jpeg', 'jpg') and data.startswith(b'\xff\xd8\xff'))
            or (fmt == 'webp' and data.startswith(b'RIFF') and data[8:12] == b'WEBP')
            or (fmt == 'gif' and (data.startswith(b'GIF87a') or data.startswith(b'GIF89a')))
        )
        if not valid_magic:
            raise HTTPException(400, '图片文件内容与格式不匹配')
        ext = 'jpg' if fmt == 'jpeg' else fmt
        filename = f'{secrets.token_hex(12)}.{ext}'
        target = folder / filename
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        saved.append(filename)
    return saved


class Prompt(BaseModel):
    text: str = Field(default='', max_length=64000)
    request_id: str = Field(min_length=16, max_length=100)
    engine: EngineType | None = None
    model: str | None = Field(default=None, max_length=120)
    effort: EffortType | None = None
    mode: ModeType | None = None
    fast: bool | None = None
    images: list[str] = Field(default_factory=list, max_length=4)
    use_worktree: bool | None = None
    execution_target: Literal['local', 'cloud'] = 'local'
    cloud_environment: str = Field(default='', max_length=200)


@app.post('/api/sessions/{ident}/messages')
async def send_message(ident: str, body: Prompt):
    s = session(ident)
    quota_wait.cancel_quota_wait(ident, reason='user_sent_message')
    prompt_text = body.text
    if not prompt_text.strip():
        if body.images:
            prompt_text = '请结合附加图片分析并处理。'
        else:
            raise HTTPException(400, '任务内容不能为空')
    chosen_engine = body.engine if (body.engine and agents.is_valid_engine(body.engine)) else s.get('engine', 'codex')
    if body.execution_target == 'cloud':
        if chosen_engine != 'codex':
            raise HTTPException(400, '此 Agent 请通过已配置的云端入口打开')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', body.cloud_environment):
            raise HTTPException(400, '请填写有效的 Codex 云端环境 ID')
        if body.images:
            raise HTTPException(400, '当前 Codex 云端提交入口暂不支持图片，请使用文字任务')
    if not agents.is_agent_installed(chosen_engine):
        raise HTTPException(409, f'{chosen_engine} 尚未安装或未配置可执行路径')

    chosen_model = clean_model(body.model) if body.model is not None else s.get('model')
    chosen_effort = body.effort if body.effort is not None else s.get('effort')
    # Never run an engine with another engine's model (e.g. a GPT id left over on an Antigravity relay session).
    chosen_model, chosen_effort = engines.resolve_model(chosen_engine, chosen_model, chosen_effort)
    chosen_mode = body.mode if body.mode is not None else (s.get('mode') or 'standard')
    chosen_fast = int(body.fast if body.fast is not None else bool(s.get('fast')))

    if body.execution_target == 'cloud':
        chosen_model, chosen_effort, chosen_mode, chosen_fast = None, None, 'standard', 0

    existing = db.one('SELECT * FROM jobs WHERE request_id=?', (body.request_id,))
    if existing:
        if existing['session_id'] != ident or existing['prompt'] != prompt_text:
            raise HTTPException(409, '重复请求标识与原任务不匹配')
        if existing.get('execution_target', 'local') != body.execution_target or existing.get('cloud_environment', '') != body.cloud_environment:
            raise HTTPException(409, '重复请求的执行位置不一致，请重新提交')
        return existing

    native_codex_id = db.get_codex_native_id(s)
    native_agy_id = db.get_antigravity_native_id(s)
    if native_codex_id:
        await asyncio.to_thread(codex_sync.sync_session_events, ident)
    if native_agy_id:
        await asyncio.to_thread(antigravity_sync.sync_antigravity_session_events, ident)
    if chosen_engine == 'codex' and native_codex_id and body.execution_target == 'local':
        local_live = db.one("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting')", (ident,))
        if not local_live and await asyncio.to_thread(codex_sync.is_thread_locked, native_codex_id):
            ext_running = await asyncio.to_thread(codex_sync.is_thread_running_externally, native_codex_id, s.get('rollout_path'), None, s)
            if ext_running:
                raise HTTPException(409, 'Mac 本机 Codex 正在执行此会话，请等待当前回合完成后继续')

    if body.use_worktree is True and not s.get('use_worktree'):
        p_obj = project(s['project_id'])
        if worktree.is_git_repository(p_obj['path']) and worktree.has_valid_head(p_obj['path']):
            await asyncio.to_thread(worktree.create_session_worktree, p_obj, s)
            s = session(ident)

    saved_images = await asyncio.to_thread(_save_images, ident, body.images) if body.images else []
    job_id, now = secrets.token_hex(12), time.time()
    try:
        with db.transaction() as conn:
            previous = conn.execute('SELECT * FROM jobs WHERE request_id=?', (body.request_id,)).fetchone()
            if previous:
                if previous['session_id'] != ident or previous['prompt'] != prompt_text:
                    raise HTTPException(409, '重复请求标识与原任务不匹配')
                return dict(previous)
            s_proj = conn.execute("SELECT * FROM projects WHERE id=?", (s['project_id'],)).fetchone()
            curr_workdir = s['worktree_path'] if (s.get('use_worktree') and s.get('worktree_path')) else (s.get('cwd') or s_proj['path'])
            active_jobs = conn.execute(
                "SELECT j.id, s.id as session_id, s.use_worktree, s.worktree_path, s.cwd "
                "FROM jobs j JOIN sessions s ON j.session_id=s.id "
                "WHERE s.project_id=? AND j.status IN ('queued','running','waiting')",
                (s['project_id'],),
            ).fetchall()
            for aj in active_jobs:
                if aj['session_id'] == ident:
                    raise HTTPException(409, '当前会话已有任务正在运行，请等待完成')
                aj_workdir = aj['worktree_path'] if (aj['use_worktree'] and aj['worktree_path']) else (aj['cwd'] or s_proj['path'])
                if aj_workdir == curr_workdir:
                    raise HTTPException(409, '此项目已有任务运行，请先停止或等待完成（或为新会话启用独立 Worktree 隔离并发执行）')
            conn.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,model,effort,mode,fast,images,engine) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, ident, body.request_id, prompt_text, 'queued', chosen_model, chosen_effort, chosen_mode, chosen_fast, json.dumps(saved_images), chosen_engine),
            )
            conn.execute('UPDATE jobs SET execution_target=?,cloud_environment=? WHERE id=?', (body.execution_target, body.cloud_environment, job_id))
            conn.execute('UPDATE sessions SET execution_target=?,cloud_environment=? WHERE id=?', (body.execution_target, body.cloud_environment, ident))
            target_nid = agents.get_session_native_agent_id(s, chosen_engine)
            conn.execute(
                "UPDATE sessions SET engine=?,native_id=?,native_codex_id=COALESCE(native_codex_id,?),native_antigravity_id=COALESCE(native_antigravity_id,?),updated=?,model=?,effort=?,mode=?,fast=?,title=CASE WHEN title='新会话' THEN ? ELSE title END WHERE id=?",
                (chosen_engine, target_nid, native_codex_id, native_agy_id, now, chosen_model, chosen_effort, chosen_mode, chosen_fast, prompt_text.strip()[:40], ident),
            )
            user_payload = {
                'text': prompt_text,
                'execution_target': body.execution_target,
                'engine': chosen_engine,
                'model': chosen_model,
                'effort': chosen_effort,
                'mode': chosen_mode,
                'fast': bool(chosen_fast),
                'images': saved_images,
            }
            conn.execute('INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                         (ident, job_id, 'user', json.dumps(user_payload, ensure_ascii=False), now))
        spawn('workstation.worker', job_id)
    except HTTPException:
        for img in saved_images:
            (db.DATA / 'uploads' / ident / img).unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.IntegrityError) as exc:
        db.execute("UPDATE jobs SET status='failed',error=?,ended=? WHERE id=?", (str(exc), time.time(), job_id))
        raise HTTPException(503, '任务进程启动失败') from exc
    return db.one('SELECT * FROM jobs WHERE id=?', (job_id,))


@app.get('/api/sessions/{ident}/uploads/{filename}')
async def get_upload(ident: str, filename: str):
    session(ident)
    if not UPLOAD_NAME_RE.fullmatch(filename):
        raise HTTPException(404, '图片不存在')
    base = (db.DATA / 'uploads' / ident).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, '图片不存在')
    ext = target.suffix.lstrip('.')
    media = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp', 'gif': 'image/gif'}[ext]
    return FileResponse(target, media_type=media)


@app.get('/api/media')
async def get_media(path: str, session_id: str | None = None):
    clean = path.strip()
    if clean.startswith('file://'):
        clean = clean[7:]
    if clean.startswith('sandbox:'):
        clean = clean[len('sandbox:'):]
    candidate = Path(clean).expanduser()
    if session_id:
        current = session(session_id)
        project = db.one('SELECT path FROM projects WHERE id=?', (current['project_id'],))
        if not candidate.is_absolute() and project:
            candidate = Path(project['path']) / candidate
    target = candidate.resolve()
    if not target.is_file():
        raise HTTPException(404, '图片或媒体文件不存在')
    ext = target.suffix.lower().lstrip('.')
    media_types = {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'webp': 'image/webp',
        'gif': 'image/gif',
        'svg': 'image/svg+xml',
        'ico': 'image/x-icon',
        'bmp': 'image/bmp',
    }
    if ext not in media_types:
        raise HTTPException(400, '不支持的媒体文件类型')

    home = Path.home().resolve()
    allowed = [
        (db.DATA / 'uploads').resolve(),
        (home / '.gemini/antigravity').resolve(),
        (home / '.codex').resolve(),
        Path('/tmp').resolve(),
    ]
    for r in db.config().get('allowed_roots', []):
        with contextlib.suppress(OSError):
            allowed.append(Path(r).resolve())
    for p in db.query('SELECT path FROM projects'):
        with contextlib.suppress(OSError):
            allowed.append(Path(p['path']).resolve())

    if not any(target.is_relative_to(root) for root in allowed):
        raise HTTPException(403, '无权访问此路径的媒体文件')

    headers = {}
    if ext == 'svg':
        # Prevent stored SVG XSS in project files by enforcing a sandboxed default-src 'none' policy
        headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        headers['X-Content-Type-Options'] = 'nosniff'

    return FileResponse(target, media_type=media_types[ext], headers=headers)


@app.post('/api/sessions/{ident}/stop')
async def stop(ident: str):
    s = session(ident)
    active_jobs = db.query(
        "SELECT id, pid, stop FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting')",
        (ident,),
    )
    if active_jobs:
        already_stopping = all(j.get('stop') for j in active_jobs)
        db.execute("UPDATE jobs SET stop=1 WHERE session_id=? AND status IN ('queued','running','waiting')", (ident,))
        await asyncio.to_thread(db.reconcile, 0.0)
        if already_stopping:
            for j in active_jobs:
                pid = j.get('pid')
                if pid and pid != os.getpid():
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGTERM)
            db.execute(
                "UPDATE jobs SET status='stopped', ended=COALESCE(ended, ?), error=COALESCE(error, '已强制终止后台任务进程') "
                "WHERE session_id=? AND status IN ('queued','running','waiting')",
                (time.time(), ident),
            )

    cascade_id = db.get_antigravity_native_id(s)
    if cascade_id:
        hub = await antigravity_hub.discover_hub(auto_launch=False)
        if hub is not None:
            with contextlib.suppress(Exception):
                await hub.call('CancelCascadeInvocation', {'cascadeId': cascade_id}, timeout=8.0)
        await asyncio.to_thread(antigravity_sync.mark_external_stopped, ident, cascade_id)

    codex_id = db.get_codex_native_id(s)
    if codex_id and not active_jobs:
        await asyncio.to_thread(codex_sync.mark_external_stopped, ident, codex_id, s.get('rollout_path'))

    return {'ok': True}


def events(ident, after):
    values = db.query('SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 500', (ident, after))
    for value in values:
        value['payload'] = json.loads(value['payload'])
    return values


def _build_session_snapshot(ident: str, before: int = 0) -> dict:
    with closing(db.connect()) as conn:
        conn.execute('BEGIN')
        upper = before - 1 if before else conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE session_id=?', (ident,)).fetchone()[0]
        users = conn.execute("SELECT id FROM events WHERE session_id=? AND id<=? AND kind='user' ORDER BY id DESC LIMIT 20", (ident, upper)).fetchall()
        lower = users[-1][0] if users else 0
        rows = [dict(row) for row in conn.execute('SELECT * FROM events WHERE session_id=? AND id>=? AND id<=? ORDER BY id', (ident, lower, upper))]
        has_older = bool(conn.execute('SELECT 1 FROM events WHERE session_id=? AND id<? LIMIT 1', (ident, lower)).fetchone())
    # Completed items contain authoritative final text; replaying their deltas is redundant.
    completed = set()
    latest_steps = {}
    latest_token_event_id = None
    for row in rows:
        row['payload'] = json.loads(row['payload'])
        payload = row['payload']
        method = payload.get('method', '')
        params = payload.get('params') or {}
        if row['kind'] == 'codex' and method == 'item/completed':
            item = params.get('item') or {}
            if item.get('text') or item.get('aggregatedOutput') or item.get('summary') or item.get('content'):
                completed.add((row['job_id'], item.get('id')))
        elif row['kind'] == 'codex' and method == 'thread/tokenUsage/updated':
            latest_token_event_id = row['id']
        if row['kind'] == 'antigravity' and payload.get('step_id'):
            latest_steps[(row['job_id'], payload['step_id'])] = row['id']
    compact = []
    for row in rows:
        payload = row['payload']
        method = payload.get('method', '')
        params = payload.get('params') or {}
        if row['kind'] == 'codex':
            if method == 'thread/tokenUsage/updated' and row['id'] != latest_token_event_id:
                continue
            item_id = params.get('itemId') or (params.get('item') or {}).get('id')
            if (row['job_id'], item_id) in completed and (
                method.endswith(('delta', 'Delta', 'summaryPartAdded')) or method == 'item/started'
            ):
                continue
        if row['kind'] == 'antigravity' and payload.get('step_id') and latest_steps[(row['job_id'], payload['step_id'])] != row['id']:
            continue
        compact.append(row)
    return {'events': compact, 'cursor': upper, 'oldest': lower, 'has_older': has_older}


@app.get('/api/sessions/{ident}/snapshot')
async def session_snapshot(ident: str, before: int = 0):
    session(ident)
    return await asyncio.to_thread(_build_session_snapshot, ident, before)


@app.get('/api/sessions/{ident}/events')
async def get_events(ident: str, after: int = 0):
    session(ident)
    await _sync_session_events(ident)
    return events(ident, after)


@app.websocket('/api/sessions/{ident}/stream')
async def stream(ws: WebSocket, ident: str, after: int = 0):
    if ws.headers.get('origin') not in db.config()['origins'] or not logged_in(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    session(ident)
    await ws.accept()
    last_sent_ts = time.monotonic()
    try:
        while logged_in(ws.cookies.get(COOKIE)):
            await _sync_session_events(ident)
            batch = events(ident, after)
            now_mono = time.monotonic()
            if batch:
                after = batch[-1]['id']
                await ws.send_json({'events': batch})
                last_sent_ts = now_mono
            elif now_mono - last_sent_ts >= 15.0:
                await ws.send_json({'heartbeat': True})
                last_sent_ts = now_mono
            await asyncio.sleep(.5 if len(batch) < 500 else 0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with __import__('contextlib').suppress(RuntimeError):
            await ws.close()


@app.get('/api/sessions/{ident}/approvals')
async def approvals(ident: str):
    session(ident)
    values = db.query("SELECT a.* FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE j.session_id=? AND j.status='waiting' AND a.decision IS NULL", (ident,))
    for value in values:
        value['payload'] = json.loads(value['payload'])
    return values


class Decision(BaseModel):
    decision: Literal['accept', 'decline', 'cancel']


@app.post('/api/approvals/{ident}')
async def decide(ident: str, body: Decision):
    value = db.one('SELECT a.*,j.session_id,j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?', (ident,))
    if not value or value['status'] != 'waiting' or value['method'] == 'item/tool/requestUserInput':
        raise HTTPException(409, '审批已失效')
    session(value['session_id'])
    if not db.decide_approval(ident, body.decision):
        raise HTTPException(409, '审批已处理或任务已结束')
    return {'ok': True}


class Answers(BaseModel):
    answers: dict[str, list[str]]


@app.post('/api/approvals/{ident}/answers')
async def answer_questions(ident: str, body: Answers):
    value = db.one('SELECT a.*,j.session_id,j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?', (ident,))
    if not value or value['status'] != 'waiting' or value['method'] != 'item/tool/requestUserInput':
        raise HTTPException(409, '问题已失效')
    session(value['session_id'])
    questions = json.loads(value['payload']).get('questions', [])
    if set(body.answers) != {q['id'] for q in questions} or any(not v or not any(x.strip() for x in v) or sum(map(len, v)) > 10000 for v in body.answers.values()):
        raise HTTPException(400, '请回答所有问题')
    # Secret answers should be entered in the native client, never persisted in this transcript.
    if any(q.get('isSecret') for q in questions):
        raise HTTPException(400, '此问题含敏感输入，请使用保密代输入通道提交')
    encoded = json.dumps({'answers': {k: {'answers': v} for k, v in body.answers.items()}}, ensure_ascii=False)
    if not db.decide_approval(ident, encoded):
        raise HTTPException(409, '问题已处理或任务已结束')
    return {'ok': True}


class SecretAnswers(BaseModel):
    answers: dict[str, str]


@app.post('/api/approvals/{ident}/secret')
async def answer_secret_questions(ident: str, body: SecretAnswers):
    """Zero-trace secret submission: stores secret values in RAM only, never in SQLite."""
    value = db.one('SELECT a.*,j.session_id,j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?', (ident,))
    if not value or value['status'] != 'waiting' or value['method'] != 'item/tool/requestUserInput':
        raise HTTPException(409, '保密输入请求已失效')
    s = session(value['session_id'])
    questions = json.loads(value['payload']).get('questions', [])
    expected_ids = {q['id'] for q in questions}
    if set(body.answers) != expected_ids or any(not str(v).strip() or len(str(v)) > 4096 for v in body.answers.values()):
        raise HTTPException(400, '请填写全部必填项')
    handoff = input_handoff.create_secret_handoff(
        job_id=value['job_id'],
        session_id=s['id'],
        purpose='交互式保密输入',
        fields=questions,
    )
    if not input_handoff.submit_secret_values(handoff['id'], body.answers):
        raise HTTPException(409, '保密输入提交失败')
    encoded_ref = json.dumps({'__secret_handoff': handoff['id']}, ensure_ascii=False)
    if not db.decide_approval(ident, encoded_ref):
        input_handoff.cancel_secret_handoff(handoff['id'])
        raise HTTPException(409, '审批已处理或任务已结束')
    db.event(s['id'], value['job_id'], 'notice', {'text': '🔐 已通过零留痕通道完成保密输入。'})
    return {'ok': True, 'handoff_id': handoff['id']}


class ReviewCreateBody(BaseModel):
    engine: str | None = None
    model: str | None = Field(default=None, max_length=120)
    focus: str = Field(default='', max_length=1000)


@app.get('/api/sessions/{ident}/quota-wait')
async def get_session_quota_wait(ident: str):
    session(ident)
    return {'quota_wait': quota_wait.get_active_quota_wait(ident)}


@app.delete('/api/sessions/{ident}/quota-wait')
async def cancel_session_quota_wait(ident: str):
    session(ident)
    cancelled = quota_wait.cancel_quota_wait(ident, reason='user_cancelled')
    return {'ok': True, 'cancelled': cancelled}


@app.get('/api/sessions/{ident}/reviews')
async def list_session_reviews(ident: str):
    s = session(ident)
    p = project(s['project_id'])
    workdir = db.session_workdir(s, p)
    rows = db.query('SELECT * FROM reviews WHERE session_id=? ORDER BY created DESC LIMIT 20', (ident,))
    checked = [review.check_review_freshness(r, workdir) for r in rows]
    return {'session_id': ident, 'reviews': checked}


@app.post('/api/sessions/{ident}/review', status_code=202)
async def start_session_review(ident: str, body: ReviewCreateBody):
    s = session(ident)
    p = project(s['project_id'])
    rev_engine = body.engine if (body.engine and agents.is_valid_engine(body.engine)) else s.get('engine', 'codex')
    if not agents.is_agent_installed(rev_engine):
        raise HTTPException(409, f'{rev_engine} 尚未安装或未配置可执行路径')
    rev_model = clean_model(body.model) if body.model else s.get('model')
    rev_model, _ = engines.resolve_model(rev_engine, rev_model, None)

    rev_prompt = await asyncio.to_thread(review.build_review_prompt, s, p, body.focus)
    rev_row = review.create_review_record(s, p, rev_engine, rev_model, focus=body.focus)

    # Create a dedicated, memory-free, read-only review session so the worker NEVER resumes
    # the developer's native thread and ALWAYS executes with rev_engine in plan mode.
    rev_session_id = secrets.token_hex(12)
    job_id = secrets.token_hex(12)
    req_id = f"rev-{rev_row['id']}"
    now = time.time()
    try:
        with db.transaction() as conn:
            conn.execute(
                'INSERT INTO sessions(id,project_id,engine,title,native_id,created,updated,'
                'model,effort,mode,fast,pinned,cwd,thread_kind,parent_session_id,forked_at_job_id) '
                'VALUES(?,?,?,?,NULL,?,?,?,?,0,0,?,?,?,?,NULL)',
                (
                    rev_session_id,
                    p['id'],
                    rev_engine,
                    f"Review: {s.get('title') or ident}",
                    now,
                    now,
                    rev_model,
                    'high',
                    'plan',
                    s.get('worktree_path') or s.get('cwd'),
                    'review',
                    ident,
                ),
            )
            conn.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,model,effort,mode,fast,images) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, rev_session_id, req_id, rev_prompt, 'queued', now, rev_model, 'high', 'plan', 0, '[]'),
            )
            conn.execute('UPDATE reviews SET review_job_id=?, updated=? WHERE id=?', (job_id, now, rev_row['id']))
            conn.execute(
                'INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                (
                    ident,
                    job_id,
                    'notice',
                    json.dumps({'text': f'🔍 已在隔离只读会话启动 /review（{rev_engine}），不继承开发记忆与思考过程。'}, ensure_ascii=False),
                    now,
                ),
            )
    except sqlite3.IntegrityError:
        db.execute("UPDATE reviews SET status='failed', updated=? WHERE id=?", (time.time(), rev_row['id']))
        raise HTTPException(409, '启动独立 /review 失败')
    spawn('workstation.worker', job_id)
    return {'review': db.one('SELECT * FROM reviews WHERE id=?', (rev_row['id'],)), 'job_id': job_id, 'review_session_id': rev_session_id}


class ModelRouteBody(BaseModel):
    candidates: list[dict] = Field(min_length=1, max_length=32)
    has_images: bool = False


@app.post('/api/models/route')
async def route_model_group(body: ModelRouteBody):
    q_snap = quotas.get_cached_quotas() if hasattr(quotas, 'get_cached_quotas') else None
    ranked = model_router.rank_candidates_by_urgency(
        body.candidates, quotas_snapshot=q_snap, has_images=body.has_images
    )
    return {'selected': ranked[0] if ranked else None, 'ranked': ranked}


class AccountCreateBody(BaseModel):
    engine: Literal['codex', 'claude-code', 'antigravity']
    label: str = Field(min_length=1, max_length=80)
    home_dir: str | None = Field(default=None, max_length=300)
    priority: int = 0


class AccountQuickLoginBody(BaseModel):
    engine: Literal['codex', 'claude-code', 'antigravity']
    label: str | None = Field(default=None, max_length=80)


@app.post('/api/accounts/quick-login', status_code=201)
async def quick_login_subscription_account(body: AccountQuickLoginBody):
    try:
        return account_pool.quick_login_account(body.engine, body.label)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f'启动直接登录失败: {exc}')


@app.get('/api/accounts')
async def list_subscription_accounts(engine: str | None = None):
    rows = account_pool.list_accounts(engine=engine)
    for r in rows:
        r['authenticated'] = account_pool.is_account_authenticated(r)
        r['login_command'] = account_pool.login_command_hint(r)
    return {'accounts': rows}


@app.post('/api/accounts', status_code=201)
async def create_subscription_account(body: AccountCreateBody):
    try:
        row = account_pool.add_account(body.engine, body.label, body.home_dir, body.priority)
        row['authenticated'] = account_pool.is_account_authenticated(row)
        row['login_command'] = account_pool.login_command_hint(row)
        return row
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post('/api/accounts/{ident}/login')
async def launch_subscription_account_login(ident: str):
    try:
        return account_pool.start_account_login(ident)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, f'启动登录进程失败: {exc}')


@app.post('/api/accounts/{ident}/pause')
async def toggle_subscription_account(ident: str, paused: bool = True):
    row = account_pool.set_account_paused(ident, paused)
    if not row:
        raise HTTPException(404, '账号不存在')
    row['authenticated'] = account_pool.is_account_authenticated(row)
    return row


@app.delete('/api/accounts/{ident}')
async def delete_subscription_account(ident: str, purge_dir: bool = True):
    if not account_pool.remove_account(ident, purge_dir=purge_dir):
        raise HTTPException(404, '账号不存在')
    return {'ok': True}


class QueueItemBody(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    steer: bool = False
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    fast: bool | None = None


class QueueMergeBody(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=20)


@app.get('/api/sessions/{ident}/queue')
async def list_session_queue(ident: str):
    session(ident)
    rows = db.query(
        "SELECT * FROM session_queue WHERE session_id=? AND status='pending' ORDER BY steer DESC, created ASC",
        (ident,),
    )
    return {'session_id': ident, 'queue': rows}


@app.post('/api/sessions/{ident}/queue', status_code=201)
async def enqueue_session_message(ident: str, body: QueueItemBody):
    s = session(ident)
    qid = secrets.token_hex(8)
    now = time.time()
    db.execute(
        'INSERT INTO session_queue(id,session_id,prompt,engine,model,effort,mode,fast,images,steer,status,created,updated) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            qid, ident, body.text.strip(), s.get('engine'),
            body.model or s.get('model'), body.effort or s.get('effort'),
            body.mode or s.get('mode'), int(body.fast if body.fast is not None else bool(s.get('fast'))),
            '[]', 1 if body.steer else 0, 'pending', now, now,
        ),
    )
    return db.one('SELECT * FROM session_queue WHERE id=?', (qid,))


@app.patch('/api/queue/{ident}')
async def update_queued_message(ident: str, body: QueueItemBody):
    row = db.one("SELECT * FROM session_queue WHERE id=? AND status='pending'", (ident,))
    if not row:
        raise HTTPException(404, '排队消息不存在或已被消费')
    db.execute(
        'UPDATE session_queue SET prompt=?, steer=?, updated=? WHERE id=?',
        (body.text.strip(), 1 if body.steer else 0, time.time(), ident),
    )
    return db.one('SELECT * FROM session_queue WHERE id=?', (ident,))


@app.delete('/api/queue/{ident}')
async def cancel_queued_message(ident: str):
    cur = db.execute("UPDATE session_queue SET status='cancelled', updated=? WHERE id=? AND status='pending'", (time.time(), ident))
    if not cur:
        raise HTTPException(404, '排队消息不存在或已被消费')
    return {'ok': True}


@app.post('/api/sessions/{ident}/queue/merge')
async def merge_session_queue(ident: str, body: QueueMergeBody):
    session(ident)
    placeholders = ','.join('?' for _ in body.ids)
    rows = db.query(
        f"SELECT * FROM session_queue WHERE session_id=? AND status='pending' AND id IN ({placeholders}) ORDER BY created ASC",
        (ident, *body.ids),
    )
    if len(rows) < 2:
        raise HTTPException(400, '至少需要两条待发送的排队消息才能合并')
    merged_text = '\n\n'.join(r['prompt'] for r in rows)
    keep = rows[0]
    now = time.time()
    with db.transaction() as conn:
        conn.execute('UPDATE session_queue SET prompt=?, updated=? WHERE id=?', (merged_text, now, keep['id']))
        for r in rows[1:]:
            conn.execute("UPDATE session_queue SET status='merged', updated=? WHERE id=?", (now, r['id']))
    return db.one('SELECT * FROM session_queue WHERE id=?', (keep['id'],))


class ForkBody(BaseModel):
    job_id: str | None = None
    title: str | None = Field(default=None, max_length=160)


@app.post('/api/sessions/{ident}/fork', status_code=201)
async def fork_session_endpoint(ident: str, body: ForkBody):
    session(ident)
    try:
        return fork.fork_session_at_job(ident, target_job_id=body.job_id, title=body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get('/api/worktrees/recycle-journal')
async def get_worktree_recycle_journal():
    return {'items': worktree.list_recycle_journal()}


@app.post('/api/worktrees/recycle-journal/{ident}/keep')
async def keep_recycled_worktree(ident: str):
    row = worktree.mark_recycle_kept(ident)
    if not row:
        raise HTTPException(404, '记录不存在')
    return row


@app.post('/api/webhooks/github')
async def github_doorbell_webhook(request: Request):
    cfg = db.config()
    secret = str(cfg.get('github_webhook_secret') or '').strip()
    if not secret:
        raise HTTPException(403, '未配置 github_webhook_secret')
    raw = await request.body()
    sig = request.headers.get('x-hub-signature-256', '')
    if not webhooks.verify_github_signature(secret, raw, sig):
        raise HTTPException(401, 'Webhook 签名无效')
    event_name = request.headers.get('x-github-event', 'unknown')
    delivery_id = request.headers.get('x-github-delivery', '')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except Exception:
        raise HTTPException(400, 'Invalid JSON')
    meta = webhooks.extract_doorbell_metadata(event_name, payload, delivery_id)
    gate = webhooks.record_and_check_doorbell(delivery_id, meta)
    return {'ok': True, 'gate': gate, 'doorbell': meta}


@app.post('/api/reviews/{ident}/apply', status_code=202)
async def apply_review_findings(ident: str):
    rev = db.one('SELECT * FROM reviews WHERE id=?', (ident,))
    if not rev:
        raise HTTPException(404, '复核记录不存在')
    s = session(rev['session_id'])
    p = project(rev['project_id'])
    rev = review.check_review_freshness(rev, db.session_workdir(s, p))
    if rev['status'] != 'completed':
        raise HTTPException(409, f"当前复核状态为 {rev['status']}，已过期或未完成，不可回流修复（Fail-Closed）")
    fix_prompt = review.build_fix_prompt_from_review(rev)
    job_id = secrets.token_hex(12)
    req_id = f"fix-{ident}-{secrets.token_hex(4)}"
    now = time.time()
    try:
        with db.transaction() as conn:
            conn.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,model,effort,mode,fast,images) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, s['id'], req_id, fix_prompt, 'queued', now, s.get('model'), s.get('effort'), s.get('mode') or 'standard', int(bool(s.get('fast'))), '[]'),
            )
            conn.execute('UPDATE reviews SET applied=1, updated=? WHERE id=?', (now, ident))
    except sqlite3.IntegrityError:
        raise HTTPException(409, '当前会话已有任务正在执行')
    spawn('workstation.worker', job_id)
    return {'ok': True, 'job_id': job_id}


def get_watch_token() -> str:
    path = db.DATA / 'watch_token.txt'
    if path.is_file():
        t = path.read_text(encoding='utf-8').strip()
        if t:
            return t
    token = secrets.token_urlsafe(24)
    db.write_private(path, token + '\n')
    return token


def rotate_watch_token() -> str:
    path = db.DATA / 'watch_token.txt'
    token = secrets.token_urlsafe(24)
    db.write_private(path, token + '\n')
    return token


def verify_watch_token(token: str | None) -> bool:
    if not token or not isinstance(token, str):
        return False
    expected = get_watch_token()
    return hmac.compare_digest(token.strip(), expected)


class WatchEinkDecision(BaseModel):
    approval_id: str | None = None
    decision: Literal['accept', 'decline', 'cancel']


def _active_approval_for_job(active_job: dict | None) -> dict | None:
    if not active_job or active_job.get('status') != 'waiting':
        return None
    appr = db.one(
        "SELECT * FROM approvals WHERE job_id=? AND decision IS NULL ORDER BY created DESC LIMIT 1",
        (active_job['id'],),
    )
    if not appr:
        return None
    try:
        payload = json.loads(appr['payload']) if isinstance(appr['payload'], str) else (appr['payload'] or {})
    except Exception:
        payload = {}
    cmd = payload.get('command') or payload.get('reason') or ''
    if isinstance(cmd, list):
        cmd = ' '.join(str(x) for x in cmd)
    cwd = payload.get('cwd') or active_job.get('cwd') or ''
    return {
        'id': appr['id'],
        'job_id': appr['job_id'],
        'method': appr['method'],
        'command': cmd,
        'reason': payload.get('reason') or '',
        'cwd': cwd,
        'is_question': appr['method'] == 'item/tool/requestUserInput',
        'created': appr['created'],
    }


def _commit_token_approval(approval_id: str | None, decision: str) -> dict:
    resolved_id = (approval_id or '').strip()
    if not resolved_id:
        latest = db.one(
            "SELECT a.id FROM approvals a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.status='waiting' AND a.decision IS NULL ORDER BY a.created DESC LIMIT 1"
        )
        if not latest:
            raise HTTPException(409, '当前没有待审批操作')
        resolved_id = latest['id']
    value = db.one(
        "SELECT a.*, j.session_id, j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (resolved_id,),
    )
    if not value or value['status'] != 'waiting':
        raise HTTPException(409, '审批已失效或任务已结束')
    if value['decision'] is not None:
        raise HTTPException(409, '该审批已被处理')
    if value['method'] == 'item/tool/requestUserInput':
        raise HTTPException(409, '请在 iPhone 或网页端回答此问题')
    if not db.decide_approval(resolved_id, decision):
        raise HTTPException(409, '审批已处理或任务已结束')
    msg = '✓ 已批准执行' if decision == 'accept' else '✕ 已拒绝操作'
    return {'ok': True, 'approval_id': resolved_id, 'decision': decision, 'message': msg}


@app.get('/api/watch/status')
@app.get('/api/watch/approve')
async def watch_deprecated_endpoints():
    raise HTTPException(410, '此接口及原快捷指令已永久下线注销，已消除所有审批与控制风险')


@app.get('/api/watch/pending')
async def watch_pending(token: str | None = None, format: str = 'json'):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')
    active_job = db.one(
        "SELECT j.*, s.title, s.cwd, s.engine FROM jobs j "
        "JOIN sessions s ON s.id=j.session_id "
        "WHERE j.status='waiting' ORDER BY j.started DESC LIMIT 1"
    )
    appr = _active_approval_for_job(active_job)
    if format == 'text':
        if not appr:
            return Response(content='当前没有待审批操作', media_type='text/plain; charset=utf-8')
        title = (active_job or {}).get('title') or '当前会话'
        cmd = appr.get('command') or appr.get('reason') or '请求执行操作'
        return Response(
            content=f"待审批 · {title}\n操作：{cmd}\nID：{appr['id']}",
            media_type='text/plain; charset=utf-8',
        )
    return {
        'pending': bool(appr),
        'session_title': (active_job or {}).get('title') if active_job else None,
        'approval': appr,
    }


@app.post('/api/watch/approve')
async def watch_approve(body: WatchEinkDecision, token: str | None = None):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')
    return _commit_token_approval(body.approval_id, body.decision)


@app.get('/api/watch/quotas')
async def watch_quotas(token: str | None = None, format: str = 'text'):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')

    q_data = await quotas.get_all_quotas(force=False)

    if format == 'json':
        return q_data

    lines = ['📊 【Codex 模型配额】']
    codex = q_data.get('codex') or {}
    if codex.get('primary'):
        p = codex['primary']
        lines.append(f"• 5小时额度：{p.get('remaining_percent')}% (剩余 {p.get('countdown_text')})")
        if p.get('reset_time_formatted'):
            lines.append(f"  ↳ 刷新：{p['reset_time_formatted']}")
    if codex.get('secondary'):
        s = codex['secondary']
        lines.append(f"• 每周额度：{s.get('remaining_percent')}% (剩余 {s.get('countdown_text')})")
        if s.get('reset_time_formatted'):
            lines.append(f"  ↳ 刷新：{s['reset_time_formatted']}")
    credits = codex.get('reset_credits', {}).get('available_count', 0)
    if credits > 0:
        lines.append(f"• 应急重置券：{credits} 张可用")

    lines.append('')
    lines.append('✨ 【Antigravity 旗舰高阶模型】')
    agy = q_data.get('antigravity') or {}
    argon = agy.get('gemini_4_argon')
    if argon:
        w = argon.get('weekly') or {}
        r5 = argon.get('rolling_5h') or {}
        lines.append(f"• {argon.get('base_name', 'Gemini 4 Argon')}：")
        lines.append(f"  - 每周配额：{w.get('remaining_percent', argon.get('remaining_percent'))}% (剩余 {w.get('countdown_text', argon.get('countdown_text'))})")
        if w.get('reset_time_formatted'):
            lines.append(f"    ↳ 周刷新：{w['reset_time_formatted']}")
        if r5 and r5.get('remaining_percent') is not None:
            lines.append(f"  - 5小时额度：{r5.get('remaining_percent')}% (剩余 {r5.get('countdown_text')})")

    flash = agy.get('general_flash')
    if flash:
        r5 = flash.get('rolling_5h') or flash
        w = flash.get('weekly') or {}
        lines.append('')
        lines.append('⚡ 【Antigravity 通用模型配额】')
        lines.append(f"• {flash.get('base_name', 'Gemini 3.8 Flash')}：")
        if w and w.get('remaining_percent') is not None:
            lines.append(f"  - 每周配额：{w['remaining_percent']}% (剩余 {w.get('countdown_text', flash.get('countdown_text'))})")
            if w.get('reset_time_formatted'):
                lines.append(f"    ↳ 周刷新：{w['reset_time_formatted']}")
        elif w.get('status_text'):
            lines.append(f"  - 每周额度：{w['status_text']}")
        if r5 and r5.get('remaining_percent') is not None:
            lines.append(f"  - 5小时额度：{r5.get('remaining_percent')}% (剩余 {r5.get('countdown_text')})")
            if r5.get('reset_time_formatted'):
                lines.append(f"    ↳ 刷新：{r5['reset_time_formatted']}")

    claude_gpt = agy.get('claude_gpt')
    if claude_gpt:
        r5 = claude_gpt.get('rolling_5h') or {}
        w = claude_gpt.get('weekly') or {}
        lines.append('')
        lines.append('🌟 【Antigravity Claude & GPT 模型配额】')
        lines.append(f"• {claude_gpt.get('base_name', 'Claude & GPT')}：")
        if w and w.get('remaining_percent') is not None:
            lines.append(f"  - 每周配额：{w['remaining_percent']}% (剩余 {w.get('countdown_text', claude_gpt.get('countdown_text'))})")
            if w.get('reset_time_formatted'):
                lines.append(f"    ↳ 周刷新：{w['reset_time_formatted']}")
        if r5 and r5.get('remaining_percent') is not None:
            lines.append(f"  - 5小时额度：{r5.get('remaining_percent')}% (剩余 {r5.get('countdown_text')})")
            if r5.get('reset_time_formatted'):
                lines.append(f"    ↳ 刷新：{r5['reset_time_formatted']}")

    # Prompt / Flow Credits
    credits = agy.get('credits') or {}
    p_avail = credits.get('available_prompt_credits')
    if p_avail is not None:
        lines.append(f"• 可用 Prompt 配额：{p_avail:,}")

    return Response(content="\n".join(lines), media_type="text/plain; charset=utf-8")


@app.get('/api/watch/live/state')
async def watch_live_state(token: str | None = None, after: int = 0):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')

    active_job = db.one(
        "SELECT j.*, s.title, s.cwd, s.engine FROM jobs j "
        "JOIN sessions s ON s.id=j.session_id "
        "WHERE j.status IN ('queued', 'running', 'waiting') "
        "ORDER BY j.started DESC LIMIT 1"
    )

    session_meta = {}
    if active_job:
        sid = active_job['session_id']
        job_data = {
            'id': active_job['id'],
            'status': active_job['status'],
            'model': active_job.get('model'),
            'effort': active_job.get('effort'),
            'started': active_job.get('started'),
            'elapsed_seconds': int(time.time() - (active_job.get('started') or time.time())),
            'engine': active_job.get('engine') or 'codex',
        }
        title = active_job.get('title') or '未命名任务'
        session_meta = db.one("SELECT engine, model, effort, updated FROM sessions WHERE id=?", (sid,)) or {}
    else:
        latest_session = db.one("SELECT s.* FROM sessions s ORDER BY s.updated DESC LIMIT 1")
        sid = latest_session['id'] if latest_session else None
        job_data = None
        title = latest_session.get('title') if latest_session else '工作台空闲'
        session_meta = latest_session or {}
    last_job = db.one(
        "SELECT status, ended, error FROM jobs WHERE session_id=? ORDER BY COALESCE(ended, started) DESC LIMIT 1",
        (sid,)
    ) if sid else None

    events_list = []
    if sid:
        await _sync_session_events(sid)
        if after == 0:
            raw_events = db.query(
                "SELECT * FROM (SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT 40) ORDER BY id ASC",
                (sid,)
            )
        else:
            raw_events = db.query(
                "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 100",
                (sid, after)
            )
        for ev in raw_events:
            try:
                ev['payload'] = json.loads(ev['payload']) if isinstance(ev['payload'], str) else ev['payload']
            except Exception:
                ev['payload'] = {}
            events_list.append(ev)

    active_approval = _active_approval_for_job(active_job) if active_job else None
    return {
        'active': bool(active_job),
        'session_id': sid,
        'session_title': title,
        'engine': (job_data or {}).get('engine') or session_meta.get('engine') or 'codex',
        'model': (job_data or {}).get('model') or session_meta.get('model'),
        'last_job': last_job,
        'server_time': time.time(),
        'job': job_data,
        'approval': active_approval,
        'events': events_list,
    }


@app.get('/watch/live.js')
async def watch_live_script():
    js_file = Path(__file__).resolve().parent / 'watch_live.js'
    return FileResponse(js_file, media_type='application/javascript; charset=utf-8')


@app.get('/watch/live')
@app.get('/api/watch/live')
async def watch_live_page(token: str | None = None):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')
    html_file = Path(__file__).resolve().parent / 'watch_view.html'
    return FileResponse(html_file, media_type='text/html; charset=utf-8')


@app.post('/api/watch/rotate')
async def watch_rotate(request: Request):
    rotate_watch_token()
    return await watch_config(request)


@app.get('/api/watch/config')
async def watch_config(request: Request):
    token = get_watch_token()
    cfg_base = (db.config().get('public_base_url') or '').strip().rstrip('/')
    if cfg_base:
        base_url = cfg_base
    else:
        origin = (request.headers.get('origin') or '').strip().rstrip('/')
        host = (request.headers.get('host') or '').strip()
        if origin and not any(local in origin for local in ('localhost', '127.0.0.1')):
            base_url = origin
        elif host and not any(local in host for local in ('localhost', '127.0.0.1')):
            scheme = request.headers.get('x-forwarded-proto') or request.url.scheme or 'https'
            base_url = f'{scheme}://{host}'
        else:
            base_url = (db.config().get('lan_url') or '').strip().rstrip('/') or 'http://127.0.0.1:8765'
    return {
        'token': token,
        'quotas_url': f"{base_url}/api/watch/quotas?token={token}",
        'live_url': f"{base_url}/watch/live?token={token}",
        'pending_url': f"{base_url}/api/watch/pending?token={token}",
        'approve_url': f"{base_url}/api/watch/approve?token={token}",
        'eink_url': f"{base_url}/eink?token={token}",
    }


EinkDecision = WatchEinkDecision


@app.get('/eink/live.js')
async def eink_live_script():
    js_file = Path(__file__).resolve().parent / 'eink_live.js'
    return FileResponse(js_file, media_type='application/javascript; charset=utf-8')


@app.get('/eink')
@app.get('/api/eink')
async def eink_live_page(token: str | None = None):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')
    html_file = Path(__file__).resolve().parent / 'eink_view.html'
    return FileResponse(html_file, media_type='text/html; charset=utf-8')


@app.get('/api/eink/state')
async def eink_live_state(token: str | None = None, after: int = 0):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')

    active_job = db.one(
        "SELECT j.*, s.title, s.cwd, s.engine, s.project_id, s.git_branch, p.name as project_name FROM jobs j "
        "JOIN sessions s ON s.id=j.session_id "
        "LEFT JOIN projects p ON p.id=s.project_id "
        "WHERE j.status IN ('queued', 'running', 'waiting') "
        "ORDER BY j.started DESC LIMIT 1"
    )

    session_meta = {}
    sid = None
    job_data = None
    active_approval = None

    if active_job:
        sid = active_job['session_id']
        job_data = {
            'id': active_job['id'],
            'status': active_job['status'],
            'model': active_job.get('model'),
            'effort': active_job.get('effort'),
            'started': active_job.get('started'),
            'elapsed_seconds': int(time.time() - (active_job.get('started') or time.time())),
            'engine': active_job.get('engine') or 'codex',
        }
        title = active_job.get('title') or '未命名任务'
        session_meta = db.one("SELECT engine, model, effort, updated, project_id, git_branch, cwd FROM sessions WHERE id=?", (sid,)) or {}

        # Look for pending approval if in waiting status
        if active_job['status'] == 'waiting':
            appr = db.one(
                "SELECT * FROM approvals WHERE job_id=? AND decision IS NULL ORDER BY created DESC LIMIT 1",
                (active_job['id'],)
            )
            if appr:
                try:
                    payload = json.loads(appr['payload']) if isinstance(appr['payload'], str) else (appr['payload'] or {})
                except Exception:
                    payload = {}
                cmd = payload.get('command') or payload.get('reason') or ''
                cwd = payload.get('cwd') or active_job.get('cwd') or ''
                active_approval = {
                    'id': appr['id'],
                    'job_id': appr['job_id'],
                    'method': appr['method'],
                    'command': cmd,
                    'reason': payload.get('reason') or '',
                    'cwd': cwd,
                    'created': appr['created'],
                }
    else:
        latest_session = db.one(
            "SELECT s.*, p.name as project_name FROM sessions s "
            "LEFT JOIN projects p ON p.id=s.project_id "
            "ORDER BY s.updated DESC LIMIT 1"
        )
        sid = latest_session['id'] if latest_session else None
        title = latest_session.get('title') if latest_session else '工作台空闲'
        session_meta = latest_session or {}

    events_list = []
    if sid:
        await _sync_session_events(sid)
        if after == 0:
            raw_events = db.query(
                "SELECT * FROM (SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT 20) ORDER BY id ASC",
                (sid,)
            )
        else:
            raw_events = db.query(
                "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 50",
                (sid, after)
            )
        for ev in raw_events:
            try:
                ev['payload'] = json.loads(ev['payload']) if isinstance(ev['payload'], str) else ev['payload']
            except Exception:
                ev['payload'] = {}
            events_list.append(ev)

    project_name = (active_job.get('project_name') if active_job else session_meta.get('project_name')) or ''
    git_branch = (active_job.get('git_branch') if active_job else session_meta.get('git_branch')) or ''
    cwd = (active_job.get('cwd') if active_job else session_meta.get('cwd')) or ''

    return {
        'active': bool(active_job),
        'session_id': sid,
        'session_title': title,
        'project_name': project_name,
        'git_branch': git_branch,
        'cwd': cwd,
        'engine': (job_data or {}).get('engine') or session_meta.get('engine') or 'codex',
        'model': (job_data or {}).get('model') or session_meta.get('model'),
        'server_time': time.time(),
        'job': job_data,
        'approval': active_approval,
        'events': events_list,
    }


@app.post('/api/eink/approve')
async def eink_approve(body: EinkDecision, token: str | None = None):
    if not verify_watch_token(token):
        raise HTTPException(401, 'Token 无效或已过期')

    value = db.one(
        "SELECT a.*, j.session_id, j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (body.approval_id,)
    )
    if not value or value['status'] != 'waiting':
        raise HTTPException(409, '审批已失效或任务已结束')
    if value['decision'] is not None:
        raise HTTPException(409, '该审批已被处理')

    if value['method'] == 'item/tool/requestUserInput':
        raise HTTPException(409, '请在网页端回答此问题')
    if not db.decide_approval(body.approval_id, body.decision):
        raise HTTPException(409, '审批已处理或任务已结束')
    return {'ok': True, 'approval_id': body.approval_id, 'decision': body.decision}


@app.get('/api/sessions/{ident}/terminal')
async def get_terminal(ident: str):
    session(ident)
    await asyncio.to_thread(db.reconcile, 2.0)
    return db.one('SELECT * FROM terminals WHERE session_id=? ORDER BY created DESC LIMIT 1', (ident,))


@app.post('/api/sessions/{ident}/terminal')
async def new_terminal(ident: str):
    session(ident)
    with db.transaction() as conn:
        existing = conn.execute("SELECT * FROM terminals WHERE session_id=? AND status IN ('starting','running')", (ident,)).fetchone()
        if existing:
            return dict(existing)
        term = secrets.token_hex(12)
        conn.execute('INSERT INTO terminals VALUES(?,?,?,NULL,?)', (term, ident, 'starting', time.time()))
    try:
        spawn('workstation.terminal', term)
    except OSError:
        db.execute("UPDATE terminals SET status='exited' WHERE id=?", (term,))
        raise
    return db.one('SELECT * FROM terminals WHERE id=?', (term,))


def terminal(ident):
    value = db.one('SELECT * FROM terminals WHERE id=?', (ident,))
    if not value:
        raise HTTPException(404, '终端不存在')
    session(value['session_id'])
    return value


@app.get('/api/terminals/{ident}/output')
async def terminal_output(ident: str, after: int = 0):
    t = terminal(ident)
    return {'status': t['status'], 'chunks': db.query('SELECT id,data FROM terminal_output WHERE terminal_id=? AND id>? ORDER BY id LIMIT 200', (ident, after))}


class TerminalInput(BaseModel):
    kind: Literal['data', 'resize', 'stop']
    data: str = Field(default='', max_length=32768)


@app.post('/api/terminals/{ident}/input')
async def terminal_input(ident: str, body: TerminalInput):
    t = terminal(ident)
    if t['status'] not in ('starting', 'running'):
        raise HTTPException(409, '终端已退出，请重新启动')
    if body.kind == 'resize':
        try:
            size = json.loads(body.data)
            if not all(type(size[k]) is int and 2 <= size[k] <= 500 for k in ('rows', 'cols')):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, '终端尺寸无效')
    if body.kind == 'data':
        try:
            base64.b64decode(body.data, validate=True)
        except ValueError:
            raise HTTPException(400, '终端输入无效')
    db.execute('INSERT INTO terminal_input(terminal_id,kind,data) VALUES(?,?,?)', (ident, body.kind, body.data))
    return {'ok': True}


def _project_base(p, session_id: str | None = None) -> Path:
    if session_id:
        s = db.one('SELECT * FROM sessions WHERE id=? AND project_id=?', (session_id, p['id']))
        if s:
            return Path(db.session_workdir(s, p)).resolve()
    return Path(p['path']).resolve()


def safe_file(p, relative: str, session_id: str | None = None):
    p_obj = Path(relative)
    if p_obj.is_absolute():
        raise HTTPException(403, '禁止使用绝对路径访问文件')
    parts = p_obj.parts
    if any(part.startswith('.') or part in ('node_modules', '__pycache__') for part in parts):
        raise HTTPException(403, '此目录或文件不在代码浏览范围内')
    base = _project_base(p, session_id)
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(403, '文件超出项目目录')
    return path


def _list_folder_files(base: Path, folder: Path):
    return [
        {'name': f.name, 'directory': f.is_dir(), 'path': str(f.relative_to(base))}
        for f in sorted(folder.iterdir(), key=lambda f: (not f.is_dir(), f.name))
        if not f.name.startswith('.') and f.name not in ('node_modules', '__pycache__') and not f.is_symlink()
    ][:500]


@app.get('/api/projects/{ident}/files')
async def files(ident: str, path: str = '', session_id: str | None = None):
    p = project(ident)
    base = _project_base(p, session_id)
    folder = safe_file(p, path, session_id)
    if not folder.is_dir():
        raise HTTPException(404, '目录不存在')
    return await asyncio.to_thread(_list_folder_files, base, folder)


def _walk_project_files(base: Path, query_text: str):
    needle = query_text.strip().lower()
    matches = []
    visited = 0
    queue = collections.deque([base])
    while queue and len(matches) < 60 and visited < 5000:
        current = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda f: (not f.is_dir(), f.name))
        except OSError:
            continue
        for entry in children:
            visited += 1
            if entry.name.startswith('.') or entry.name in SKIP_DIRS or entry.is_symlink():
                continue
            resolved = entry.resolve()
            if not resolved.is_relative_to(base):
                continue
            rel = str(resolved.relative_to(base))
            if entry.is_dir():
                queue.append(resolved)
            elif entry.is_file():
                if not needle or needle in rel.lower():
                    matches.append({'name': entry.name, 'path': rel})
                    if len(matches) >= 60:
                        break
    return matches


@app.get('/api/projects/{ident}/search_files')
async def search_files(ident: str, q: str = '', session_id: str | None = None):
    p = project(ident)
    base = _project_base(p, session_id)
    return await asyncio.to_thread(_walk_project_files, base, q[:120])


def _read_text_file_checked(file: Path, rel_path: str):
    if not file.is_file():
        raise HTTPException(404, '文件不存在')
    if file.stat().st_size > 1024 * 1024:
        raise HTTPException(413, '文件超过 1 MB，请在终端查看')
    raw = file.read_bytes()
    if b'\x00' in raw:
        raise HTTPException(415, '二进制文件请在终端查看')
    return {'path': rel_path, 'text': raw.decode('utf-8', errors='replace')}


@app.get('/api/projects/{ident}/file')
async def read_file(ident: str, path: str, session_id: str | None = None):
    file = safe_file(project(ident), path, session_id)
    return await asyncio.to_thread(_read_text_file_checked, file, path)


def git_run(path, *args):
    result = subprocess.run(['git', '-C', path, *args], capture_output=True, timeout=15)
    return result.returncode, result.stdout[:2 * 1024 * 1024].decode(errors='replace')


@app.get('/api/projects/{ident}/diff')
async def diff(ident: str, session_id: str | None = None):
    p = project(ident)
    workdir = str(_project_base(p, session_id))
    status, patch, staged, branch, head = await asyncio.gather(
        asyncio.to_thread(git_run, workdir, 'status', '--short', '--', '.'),
        asyncio.to_thread(git_run, workdir, 'diff', '--no-ext-diff', '--no-color', '--', '.'),
        asyncio.to_thread(git_run, workdir, 'diff', '--cached', '--no-ext-diff', '--no-color', '--', '.'),
        asyncio.to_thread(git_run, workdir, 'rev-parse', '--abbrev-ref', 'HEAD'),
        asyncio.to_thread(git_run, workdir, 'log', '-1', '--pretty=format:%h · %s'),
    )
    is_git = status[0] == 0
    return {
        'is_git': is_git,
        'workdir': workdir,
        'branch': branch[1].strip() if is_git and branch[0] == 0 else '',
        'head': head[1].strip() if is_git and head[0] == 0 else '',
        'status': status[1],
        'diff': patch[1],
        'staged': staged[1],
    }


@app.api_route('/healthz', methods=['GET', 'HEAD'])
async def health(cf_auth: bool = False):
    # This path bypasses even older PWA workers, allowing a top-level Access challenge.
    if cf_auth:
        return RedirectResponse('/', status_code=303)
    return {'ok': True, 'service': 'walking-workstation'}


class PushKeysBody(BaseModel):
    p256dh: str = Field(min_length=40, max_length=200)
    auth: str = Field(min_length=12, max_length=100)


class PushSubscribeBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2048)
    keys: PushKeysBody
    device_label: str = Field(default='', max_length=120)


class PushUnsubscribeBody(BaseModel):
    endpoint: str | None = Field(default=None, max_length=2048)
    id: str | None = Field(default=None, max_length=64)


class PushTestBody(BaseModel):
    endpoint: str | None = Field(default=None, max_length=2048)
    session_id: str | None = Field(default=None, max_length=64)
    kind: Literal['test', 'approval'] = 'test'


@app.get('/api/push/status')
async def push_status():
    return webpush.get_push_status()


@app.post('/api/push/subscribe')
async def push_subscribe(body: PushSubscribeBody, request: Request):
    try:
        return webpush.upsert_subscription(
            body.endpoint,
            body.keys.p256dh,
            body.keys.auth,
            device_label=body.device_label,
            user_agent=request.headers.get('user-agent', ''),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/push/unsubscribe')
async def push_unsubscribe(body: PushUnsubscribeBody):
    if not body.endpoint and not body.id:
        raise HTTPException(400, '请指定要取消订阅的设备或推送端点')
    return webpush.remove_subscription(endpoint=body.endpoint, sub_id=body.id)


@app.post('/api/push/test')
async def push_test(body: PushTestBody | None = None):
    req = body or PushTestBody()
    if req.kind == 'approval':
        result = await webpush.notify_all(
            title='Rove · 审批测试推送',
            body='测试操作：git status（用于验证 iPhone 锁屏与 Apple Watch 镜像审批提醒）',
            session_id=req.session_id,
            approval_id='test-preview',
            actions=[
                {'action': 'accept', 'title': '✓ 批准执行'},
                {'action': 'decline', 'title': '✕ 拒绝'},
            ],
            tag='workstation-approval-test',
            kind='approval',
            require_interaction=True,
            target_endpoint=req.endpoint,
        )
    else:
        result = await webpush.notify_all(
            title='Rove · 锁屏推送已连通',
            body='后台 Codex / Antigravity 任务完成或等待审批时，将实时推送到此设备。',
            session_id=req.session_id,
            tag='workstation-test',
            kind='test',
            require_interaction=False,
            target_endpoint=req.endpoint,
        )
    return {**result, 'web_push': webpush.get_push_status()}


@app.get('/api/feishu/status')
async def feishu_status():
    return feishu_relay.get_feishu_status()


@app.post('/api/feishu/webhook')
async def feishu_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, '无效的飞书事件 JSON') from exc
    try:
        return await feishu_relay.handle_webhook_payload(payload)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@app.post('/api/feishu/test')
async def feishu_test():
    status = feishu_relay.get_feishu_status()
    if not status.get('configured'):
        raise HTTPException(400, '飞书未配置 App ID 或 Secret')
    if not status.get('has_target'):
        raise HTTPException(400, '飞书尚未配对接收人（无 open_id 或 chat_id）')
    cfg = feishu_relay.load_feishu_config()
    target = feishu_relay.get_target_receive_id(cfg)
    api = feishu_relay.FeishuAPI(cfg['app_id'], cfg['app_secret'])
    try:
        await api.send_text(
            target[0],
            '🔔【Rove 工作台 · 飞书多端协同测试】\n连接正常！工作台的审批通知与任务结果将实时同步至此。',
            receive_id_type=target[1],
        )
        return {'ok': True, 'target': target[0]}
    except Exception as exc:
        raise HTTPException(502, f'发送飞书消息失败：{exc}')


class CodeAuditBody(BaseModel):
    code: str = Field(min_length=1, max_length=100000)


@app.get('/api/design/standard')
async def get_design_standard():
    return {
        'directive': design_guard.ANTI_SLOP_DIRECTIVE,
        'keywords': design_guard.UI_KEYWORDS,
    }


@app.post('/api/projects/{ident}/design/seed')
async def seed_project_design(ident: str):
    project(ident)
    created = design_guard.seed_design_memory(ident)
    return {'ok': True, 'memory': created}


@app.post('/api/design/audit')
async def audit_design_code(body: CodeAuditBody):
    findings = design_guard.audit_code_changes(body.code)
    return {'findings': findings, 'clean': len(findings) == 0}


class StoragePruneBody(BaseModel):
    keep_days: int = 14
    compact_events: bool = True
    compact_all: bool = True
    prune_sessions: bool = False
    vacuum: bool = True
    backup_before: bool = True
    dry_run: bool = False


@app.get('/api/system/storage')
async def get_system_storage():
    return {'ok': True, **retention.get_storage_stats()}


@app.post('/api/system/storage/prune')
async def prune_system_storage(body: StoragePruneBody):
    result = await asyncio.to_thread(
        retention.run_lifecycle_maintenance,
        {
            'keep_days': body.keep_days,
            'compact_events': body.compact_events,
            'compact_all': body.compact_all,
            'prune_sessions': body.prune_sessions,
            'vacuum': body.vacuum,
            'backup_before': body.backup_before,
            'dry_run': body.dry_run,
        },
    )
    return result


class AgentUpsertBody(BaseModel):
    cloud_url: str | None = Field(default=None, max_length=2048)
    id: str = Field(min_length=2, max_length=40)
    name: str | None = Field(default=None, max_length=80)
    short_name: str | None = Field(default=None, max_length=24)
    description: str | None = Field(default=None, max_length=500)
    adapter: Literal['cli', 'openai_http'] | None = None
    enabled: bool = True
    tone: str | None = Field(default=None, max_length=24)
    binary: str | None = Field(default=None, max_length=1024)
    args_template: list[str] | str | None = None
    model_flag: str | None = Field(default=None, max_length=40)
    resume_flag: str | None = Field(default=None, max_length=40)
    output_format: str | None = Field(default=None, max_length=40)
    base_url: str | None = Field(default=None, max_length=1024)
    api_key: str | None = Field(default=None, max_length=512)
    api_key_env: str | None = Field(default=None, max_length=80)
    system_prompt: str | None = Field(default=None, max_length=4000)
    models: list[dict | str] | None = None


@app.get('/api/agents')
async def get_agents():
    return {
        'agents': agents.list_all_agents(),
        'presets': agents.list_presets(),
        'catalog': engines.catalog(),
    }


@app.post('/api/agents')
async def save_agent(body: AgentUpsertBody):
    try:
        payload = body.model_dump(exclude_none=True)
        saved = agents.upsert_custom_agent(payload)
        return {
            'ok': True,
            'agent': saved,
            'agents': agents.list_all_agents(),
            'catalog': engines.catalog(),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete('/api/agents/{agent_id}')
async def remove_agent(agent_id: str):
    try:
        removed = agents.delete_custom_agent(agent_id)
        if not removed:
            raise HTTPException(404, '未找到该自定义 Agent')
        return {
            'ok': True,
            'deleted': agent_id,
            'agents': agents.list_all_agents(),
            'catalog': engines.catalog(),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/agents/{agent_id}/check')
async def check_agent_endpoint(agent_id: str):
    if agent_id == 'antigravity':
        return await engines.check_antigravity()
    if agent_id == 'codex':
        installed = Path(db.config().get('codex_bin', '')).is_file()
        return engines.record('codex', {
            'status': 'ready' if installed else 'unavailable',
            'code': None if installed else 'not_installed',
            'detail': 'Codex CLI 已就绪' if installed else '未检测到 Codex CLI',
        })
    try:
        return await agents.check_custom_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


class ClusterConfigPatchBody(BaseModel):
    node_name: str | None = Field(default=None, max_length=80)
    node_role: Literal['primary', 'pve-standby'] | None = None
    peer_url: str | None = Field(default=None, max_length=512)
    pve_ssh_host: str | None = Field(default=None, max_length=120)
    pve_ctid: str | None = Field(default=None, max_length=20)
    remote_data_dir: str | None = Field(default=None, max_length=512)
    pve_secondary_backup_dir: str | None = Field(default=None, max_length=512)
    remote_workspace_root: str | None = Field(default=None, max_length=512)
    cluster_token: str | None = Field(default=None, max_length=256)
    max_snapshots: int | None = Field(default=None, ge=2, le=50)


class ClusterSnapshotCreateBody(BaseModel):
    include_projects: bool = True
    label: str = Field(default='manual', max_length=32)


class ClusterRestoreBody(BaseModel):
    filename: str | None = Field(default=None, max_length=160)
    restore_db: bool = True
    restore_projects: bool = True
    only_missing_files: bool = False


@app.get('/api/cluster/ping')
async def cluster_ping():
    cfg = cluster_sync.get_cluster_config()
    return {
        'ok': True,
        'node_name': cfg.get('node_name'),
        'node_role': cfg.get('node_role'),
        'platform': sys.platform,
        'projects_count': (db.one('SELECT count(*) AS c FROM projects') or {}).get('c', 0),
        'sessions_count': (db.one('SELECT count(*) AS c FROM sessions') or {}).get('c', 0),
    }


@app.get('/api/cluster/status')
async def get_cluster_status():
    return await asyncio.to_thread(cluster_sync.get_cluster_status)


@app.patch('/api/cluster/config')
async def patch_cluster_config(body: ClusterConfigPatchBody):
    payload = body.model_dump(exclude_none=True)
    await asyncio.to_thread(cluster_sync.update_cluster_config, payload)
    return await asyncio.to_thread(cluster_sync.get_cluster_status)


@app.post('/api/cluster/probe')
async def probe_cluster_peer():
    return await asyncio.to_thread(cluster_sync.probe_peer_node)


@app.post('/api/cluster/snapshot')
async def create_cluster_snapshot(body: ClusterSnapshotCreateBody | None = None):
    req = body or ClusterSnapshotCreateBody()
    snap = await asyncio.to_thread(
        cluster_sync.create_snapshot,
        req.include_projects,
        None,
        req.label,
    )
    return {
        'ok': True,
        'snapshot': snap,
        'status': await asyncio.to_thread(cluster_sync.get_cluster_status),
    }


@app.get('/api/cluster/snapshot/export')
async def export_cluster_snapshot(filename: str | None = None, include_projects: bool = True):
    if filename:
        safe_name = Path(filename).name
        target = (cluster_sync.snapshots_dir() / safe_name).resolve()
        if not target.is_relative_to(cluster_sync.snapshots_dir().resolve()) or not target.is_file():
            raise HTTPException(404, '指定的快照文件不存在')
    else:
        existing = cluster_sync.list_snapshots()
        if existing:
            target = cluster_sync.snapshots_dir() / existing[0]['filename']
        else:
            snap = await asyncio.to_thread(cluster_sync.create_snapshot, include_projects, None, 'export')
            target = Path(snap['path'])
    return FileResponse(
        target,
        media_type='application/gzip',
        filename=target.name,
    )


@app.post('/api/cluster/snapshot/import')
async def import_cluster_snapshot(
    request: Request,
    restore_db: bool = True,
    restore_projects: bool = True,
    only_missing_files: bool = False,
):
    hdr_name = request.headers.get('x-snapshot-filename') or ''
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '', hdr_name)
    if not safe_name.startswith('rove-snapshot-') or not safe_name.endswith('.tar.gz'):
        safe_name = f'rove-snapshot-{int(time.time())}-imported.tar.gz'
    dest = cluster_sync.snapshots_dir() / safe_name
    tmp_dest = dest.with_suffix('.tmp-' + secrets.token_hex(4))
    total_written = 0
    try:
        MAX_SNAPSHOT_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB limit to prevent disk exhaustion
        fd = os.open(tmp_dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as out_f:
            async for chunk in request.stream():
                if chunk:
                    total_written += len(chunk)
                    if total_written > MAX_SNAPSHOT_SIZE:
                        raise HTTPException(413, '快照文件超过单次导入上限 (2 GB)')
                    await asyncio.to_thread(out_f.write, chunk)
        if total_written < 64:
            tmp_dest.unlink(missing_ok=True)
            raise HTTPException(400, '上传的快照内容为空或格式无效')
        os.replace(tmp_dest, dest)
    except BaseException:
        tmp_dest.unlink(missing_ok=True)
        raise
    mirror = {'status': 'skipped'}
    try:
        mirrored = await asyncio.to_thread(cluster_sync.mirror_snapshot_to_secondary, dest)
        if mirrored:
            mirror = {'status': 'completed', 'path': str(mirrored)}
    except Exception as exc:
        mirror = {'status': 'failed', 'error': str(exc)}
    try:
        res = await asyncio.to_thread(
            cluster_sync.restore_snapshot,
            dest,
            restore_db,
            restore_projects,
            only_missing_files,
        )
        return {**res, 'ok': res.get('ok', True) and mirror['status'] != 'failed', 'secondary_backup': mirror}
    except Exception as exc:
        raise HTTPException(400, f'恢复导入快照失败：{exc}') from exc


@app.post('/api/cluster/restore')
async def restore_local_snapshot(body: ClusterRestoreBody):
    snaps = cluster_sync.list_snapshots()
    if not snaps:
        raise HTTPException(404, '本机尚无可用快照，请先创建快照或从 PVE 节点拉取')
    chosen_name = Path(body.filename).name if body.filename else snaps[0]['filename']
    target = (cluster_sync.snapshots_dir() / chosen_name).resolve()
    if not target.is_relative_to(cluster_sync.snapshots_dir().resolve()) or not target.is_file():
        raise HTTPException(404, f'未找到快照文件：{chosen_name}')
    try:
        res = await asyncio.to_thread(
            cluster_sync.restore_snapshot,
            target,
            body.restore_db,
            body.restore_projects,
            body.only_missing_files,
        )
        return {
            **res,
            'status': await asyncio.to_thread(cluster_sync.get_cluster_status),
        }
    except Exception as exc:
        raise HTTPException(500, f'恢复快照失败：{exc}') from exc


@app.post('/api/cluster/push')
async def push_to_pve_peer(body: ClusterSnapshotCreateBody | None = None):
    req = body or ClusterSnapshotCreateBody()
    try:
        res = await asyncio.to_thread(cluster_sync.push_snapshot_to_peer, req.include_projects)
        return {
            **res,
            'status': await asyncio.to_thread(cluster_sync.get_cluster_status),
        }
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post('/api/cluster/pull')
async def pull_from_pve_peer(body: ClusterRestoreBody | None = None):
    req = body or ClusterRestoreBody()
    try:
        res = await asyncio.to_thread(
            cluster_sync.pull_and_restore_from_peer,
            req.restore_db,
            req.restore_projects,
            req.only_missing_files,
        )
        return {
            **res,
            'status': await asyncio.to_thread(cluster_sync.get_cluster_status),
        }
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


DIST = db.ROOT / 'frontend/dist'
PUBLIC_FALLBACK = db.ROOT / 'frontend/public'
if (DIST / 'assets').exists():
    app.mount('/watch/assets', StaticFiles(directory=DIST / 'assets'), name='watch_assets')
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


def _public_asset(filename: str) -> Path:
    dist_file = DIST / filename
    if dist_file.is_file():
        return dist_file
    fallback = PUBLIC_FALLBACK / filename
    if fallback.is_file():
        return fallback
    raise HTTPException(404, '静态资源不存在')


@app.api_route('/', methods=['GET', 'HEAD'])
async def index(request: Request):
    index_path = DIST / 'index.html'
    if request.method == 'HEAD' or not index_path.is_file():
        return FileResponse(index_path)
    html_text = index_path.read_text(encoding='utf-8')
    if '<!--__ROVE_BOOTSTRAP__-->' in html_text:
        try:
            cfg = db.config()
            endpoints = {
                'lan_url': (cfg.get('lan_url') or '').strip(),
                'public_url': (cfg.get('public_base_url') or '').strip(),
            }
            if logged_in(request.cookies.get(COOKIE)):
                st = await state(sync=False)
                boot_payload = {'locked': False, 'state': st, 'endpoints': endpoints, 'ts': time.time()}
            else:
                boot_payload = {'locked': True, 'endpoints': endpoints, 'ts': time.time()}
            safe_json = json.dumps(boot_payload, ensure_ascii=False).replace('</', '<\\/')
            html_text = html_text.replace(
                '<!--__ROVE_BOOTSTRAP__-->',
                f'<script>window.__ROVE_BOOTSTRAP__={safe_json};</script>',
                1,
            )
        except Exception:
            pass
    return HTMLResponse(html_text)


@app.api_route('/manifest.webmanifest', methods=['GET', 'HEAD'])
@app.api_route('/manifest.json', methods=['GET', 'HEAD'])
async def manifest():
    payload = {
        'id': '/',
        'name': 'Rove',
        'short_name': 'Rove',
        'description': 'Rove · 随身连接 Mac mini 的 Codex 与 Antigravity 双引擎编程工作台',
        'lang': 'zh-CN',
        'dir': 'ltr',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'display_override': ['window-controls-overlay', 'standalone', 'minimal-ui'],
        'orientation': 'any',
        'background_color': '#0d0f13',
        'theme_color': '#0d0f13',
        'prefer_related_applications': False,
        'categories': ['developer tools', 'productivity', 'utilities'],
        'icons': [
            {'src': '/rove-192.png?v=4', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/rove-512.png?v=4', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/rove-maskable-512.png?v=4', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': '/pwa-192.png?v=4', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/pwa-512.png?v=4', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/pwa-maskable-512.png?v=4', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': '/rove-apple-touch.png?v=4', 'sizes': '180x180', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/apple-touch-icon.png?v=4', 'sizes': '180x180', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/rove-favicon.svg?v=4', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'},
            {'src': '/favicon.svg?v=4', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'},
        ],
        'shortcuts': [
            {
                'name': '新建编程任务',
                'short_name': '新建任务',
                'description': '立即新建一个 Codex 或 Antigravity 编程任务',
                'url': '/?action=new',
                'icons': [{'src': '/rove-192.png?v=4', 'sizes': '192x192', 'type': 'image/png'}],
            },
            {
                'name': '打开本机终端',
                'short_name': '终端',
                'description': '进入 Mac mini 实时 PTY 终端',
                'url': '/?tab=terminal',
                'icons': [{'src': '/rove-192.png?v=4', 'sizes': '192x192', 'type': 'image/png'}],
            },
            {
                'name': '查看模型额度',
                'short_name': '模型额度',
                'description': '查看 Codex 与 Antigravity 实时配额及刷新时间',
                'url': '/?modal=quota',
                'icons': [{'src': '/rove-192.png?v=4', 'sizes': '192x192', 'type': 'image/png'}],
            },
        ],
    }
    return JSONResponse(payload, media_type='application/manifest+json')


@app.api_route('/sw.js', methods=['GET', 'HEAD'])
async def service_worker():
    return FileResponse(
        _public_asset('sw.js'),
        media_type='application/javascript',
        headers={'Service-Worker-Allowed': '/'},
    )


@app.api_route('/apple-touch-icon.png', methods=['GET', 'HEAD'])
@app.api_route('/apple-touch-icon-precomposed.png', methods=['GET', 'HEAD'])
@app.api_route('/rove-apple-touch.png', methods=['GET', 'HEAD'])
async def apple_touch_icon():
    for name in ('rove-apple-touch.png', 'apple-touch-icon.png'):
        try:
            return FileResponse(_public_asset(name), media_type='image/png')
        except HTTPException:
            continue
    raise HTTPException(404, '图标不存在')


@app.api_route('/pwa-192.png', methods=['GET', 'HEAD'])
@app.api_route('/rove-192.png', methods=['GET', 'HEAD'])
async def pwa_icon_192():
    for name in ('rove-192.png', 'pwa-192.png'):
        try:
            return FileResponse(_public_asset(name), media_type='image/png')
        except HTTPException:
            continue
    raise HTTPException(404, '图标不存在')


@app.api_route('/pwa-512.png', methods=['GET', 'HEAD'])
@app.api_route('/rove-512.png', methods=['GET', 'HEAD'])
async def pwa_icon_512():
    for name in ('rove-512.png', 'pwa-512.png'):
        try:
            return FileResponse(_public_asset(name), media_type='image/png')
        except HTTPException:
            continue
    raise HTTPException(404, '图标不存在')


@app.api_route('/pwa-maskable-512.png', methods=['GET', 'HEAD'])
@app.api_route('/rove-maskable-512.png', methods=['GET', 'HEAD'])
async def pwa_icon_maskable_512():
    for name in ('rove-maskable-512.png', 'pwa-maskable-512.png'):
        try:
            return FileResponse(_public_asset(name), media_type='image/png')
        except HTTPException:
            continue
    raise HTTPException(404, '图标不存在')


@app.api_route('/favicon.svg', methods=['GET', 'HEAD'])
@app.api_route('/favicon.ico', methods=['GET', 'HEAD'])
@app.api_route('/rove-favicon.svg', methods=['GET', 'HEAD'])
async def favicon():
    for name in ('rove-favicon.svg', 'favicon.svg'):
        try:
            return FileResponse(_public_asset(name), media_type='image/svg+xml')
        except HTTPException:
            continue
    raise HTTPException(404, '图标不存在')


@app.get('/api/jobs/{ident}/cloud')
async def cloud_job_details(ident: str):
    job = db.one('SELECT * FROM jobs WHERE id=?', (ident,))
    if not job or not job.get('cloud_task_id'):
        raise HTTPException(404, '未找到云端任务')
    try:
        return await cloud_execution.details(job['cloud_task_id'])
    except (RuntimeError, TimeoutError, OSError) as exc:
        raise HTTPException(502, f'查询云端任务失败：{exc}') from exc

