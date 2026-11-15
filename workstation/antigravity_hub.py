"""Connect-RPC client for the local Antigravity Desktop Hub (Gemini.app / Antigravity 2.0).

Standalone `agy` CLI uses consumer keyring OAuth without the desktop host-auth Pro bridge,
which fails `loadCodeAssist` with a region/eligibility error on restricted egress routes.
Connecting to the local authenticated `agy --hub` process launched by Gemini.app / Antigravity.app
reuses its verified host-auth session (`--use_host_auth`), supports all Pro models and images,
and keeps sessions synchronized with `~/.gemini/antigravity`.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urlparse
import uuid

import httpx

from . import store as db
from . import memories
from . import design_guard
from . import context_bridge

_SERVICE_PREFIX = '/exa.language_server_pb.LanguageServerService/'

_BUILTIN_PLAN_MODELS = {
    'gemini-4-argon-high': 'MODEL_PLACEHOLDER_M417',
    'gemini-4-argon-medium': 'MODEL_PLACEHOLDER_M418',
    'gemini-4-argon-low': 'MODEL_PLACEHOLDER_M419',
    'gemini-3.8-flash-high': 'MODEL_PLACEHOLDER_M318',
    'gemini-3.8-flash-medium': 'MODEL_PLACEHOLDER_M319',
    'gemini-3.8-flash-low': 'MODEL_PLACEHOLDER_M320',
    'claude-opus-5-5-low': 'MODEL_PLACEHOLDER_M400',
    'claude-opus-5-5-medium': 'MODEL_PLACEHOLDER_M401',
    'claude-opus-5-5-high': 'MODEL_PLACEHOLDER_M402',
    'claude-sonnet-5-5-low': 'MODEL_PLACEHOLDER_M403',
    'claude-sonnet-5-5-medium': 'MODEL_PLACEHOLDER_M404',
    'claude-sonnet-5-5-high': 'MODEL_PLACEHOLDER_M405',
    'gpt-oss-120b-medium': 'MODEL_OPENAI_GPT_OSS_120B_MEDIUM',
}

_MAX_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
_ACTIVE_CASCADE_STATUSES = {'CASCADE_RUN_STATUS_RUNNING', 'CASCADE_RUN_STATUS_BUSY'}


def _is_default_workstation_data() -> bool:
    default_data = (Path.home() / '.config' / 'walking-workstation').resolve()
    try:
        return db.DATA.resolve() == default_data
    except OSError:
        return False


def _configured_hub_url() -> str | None:
    try:
        cfg_val = db.config().get('antigravity_hub_url')
    except Exception:
        cfg_val = None
    raw = cfg_val or os.environ.get('WORKSTATION_ANTIGRAVITY_HUB_URL')
    if raw:
        return str(raw).rstrip('/')
    return None


def _scan_running_hub_urls() -> list[str]:
    """Scan local `agy --hub --hub-port=<port>` processes belonging to current user."""
    try:
        out = subprocess.check_output(['ps', '-ax', '-o', 'uid=,args='], text=True, timeout=3)
    except Exception:
        return []
    my_uid = str(os.getuid())
    urls = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2 or parts[0] != my_uid:
            continue
        args = parts[1]
        if '--hub' not in args or 'agy' not in args:
            continue
        m = re.search(r'--hub-port(?:=|\s+)(\d+)', args)
        if m:
            url = f'http://127.0.0.1:{m.group(1)}'
            if url not in urls:
                urls.append(url)
    return urls


def _desktop_host_apps() -> list[Path]:
    candidates = [
        Path('/Applications/Gemini.app'),
        Path('/Applications/Antigravity.app'),
        Path.home() / 'Applications' / 'Gemini.app',
        Path.home() / 'Applications' / 'Antigravity.app',
    ]
    return [p for p in candidates if p.exists()]


class AntigravityHubClient:
    def __init__(self, base_url: str, csrf_token: str):
        self.base_url = base_url.rstrip('/')
        self.csrf_token = csrf_token
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=30.0),
                timeout=30.0,
            )
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'Connect-Protocol-Version': '1',
            'x-codeium-csrf-token': self.csrf_token,
        }

    async def call(self, method: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
        url = f'{self.base_url}{_SERVICE_PREFIX}{method}'
        client = self._get_client()
        try:
            resp = await client.post(url, headers=self._headers, json=payload if payload is not None else {}, timeout=timeout)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout):
            await self.aclose()
            client = self._get_client()
            resp = await client.post(url, headers=self._headers, json=payload if payload is not None else {}, timeout=timeout)

        if resp.status_code != 200:
            detail = resp.text[:600]
            try:
                err_obj = resp.json()
                detail = err_obj.get('message') or detail
            except Exception:
                pass
            raise RuntimeError(f'Antigravity Hub {method} HTTP {resp.status_code}: {detail}')
        return resp.json() if resp.content else {}

    async def get_user_status(self) -> dict:
        data = await self.call('GetUserStatus', {}, timeout=10.0)
        return data.get('userStatus') or {}

    async def get_model_configs(self) -> list[dict]:
        data = await self.call('GetCascadeModelConfigData', {}, timeout=10.0)
        return data.get('clientModelConfigs') or []

    async def retrieve_user_quota_summary(self, force_refresh: bool = False) -> dict:
        data = await self.call('RetrieveUserQuotaSummary', {'forceRefresh': force_refresh}, timeout=10.0)
        return data.get('response') or {}


async def _probe_hub_url(url: str) -> AntigravityHubClient | None:
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(f'{url}/', timeout=3.0)
        if resp.status_code != 200:
            return None
        m = re.search(r'window\.__APP_CONFIG__\s*=\s*(\{.*?\});', resp.text)
        if not m:
            return None
        app_cfg = json.loads(m.group(1))
        csrf = app_cfg.get('csrfToken')
        if not csrf:
            return None
        hub = AntigravityHubClient(url, csrf)
        status = await hub.get_user_status()
        if status.get('email') or status.get('planStatus') or status.get('name'):
            return hub
    except Exception:
        return None
    return None


_cached_hub: 'AntigravityHubClient | None' = None
_cached_hub_ts: float = 0.0


async def discover_hub(auto_launch: bool = True) -> 'AntigravityHubClient | None':
    """Locate an authenticated local Antigravity Hub, optionally waking Gemini.app in background."""
    global _cached_hub, _cached_hub_ts
    now = time.monotonic()
    if _cached_hub is not None:
        if (now - _cached_hub_ts) < 30.0:
            return _cached_hub
        try:
            status = await _cached_hub.get_user_status()
            if status.get('email') or status.get('planStatus') or status.get('name'):
                _cached_hub_ts = now
                return _cached_hub
        except Exception:
            await _cached_hub.aclose()
            _cached_hub = None

    explicit = _configured_hub_url()
    if explicit:
        hub = await _probe_hub_url(explicit)
        if hub:
            _cached_hub = hub
            _cached_hub_ts = now
            return hub
        return None

    if not _is_default_workstation_data():
        return None

    running_urls = await asyncio.to_thread(_scan_running_hub_urls)
    for url in running_urls:
        hub = await _probe_hub_url(url)
        if hub:
            _cached_hub = hub
            _cached_hub_ts = now
            return hub

    if not auto_launch:
        return None

    apps = _desktop_host_apps()
    if not apps:
        return None

    try:
        subprocess.Popen(
            ['open', '-g', '-j', '-a', str(apps[0])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    deadline = time.monotonic() + 9.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.6)
        running_urls = await asyncio.to_thread(_scan_running_hub_urls)
        for url in running_urls:
            hub = await _probe_hub_url(url)
            if hub:
                _cached_hub = hub
                _cached_hub_ts = time.monotonic()
                return hub
    return None


def format_hub_models(client_configs: list[dict]) -> list[dict]:
    """Convert Hub `clientModelConfigs` into mobile-workstation catalog items."""
    items = []
    seen = set()
    # Put Gemini 4 Argon High/Medium first when present for clean default ordering.
    def _sort_key(cfg: dict):
        mid = cfg.get('modelId') or ''
        if mid == 'gemini-4-argon-high':
            return (0, mid)
        if mid == 'gemini-4-argon-medium':
            return (1, mid)
        if mid.startswith('gemini-4-'):
            return (2, mid)
        if mid.startswith('gemini-'):
            return (3, mid)
        if mid.startswith('claude-'):
            return (4, mid)
        return (5, mid)

    for cfg in sorted(client_configs, key=_sort_key):
        mid = (cfg.get('modelId') or '').strip()
        plan_enum = (cfg.get('modelOrAlias') or {}).get('model')
        if not mid or not plan_enum or mid in seen or cfg.get('disabled'):
            continue
        seen.add(mid)
        label = (cfg.get('label') or mid).strip()
        suffix = mid.rsplit('-', 1)[-1]
        inferred = suffix if suffix in _MAX_EFFORTS else 'medium'
        quota = (cfg.get('quotaInfo') or {}).get('remainingFraction')
        quota_hint = f' · 剩余额度 {round(quota * 100)}%' if isinstance(quota, (int, float)) else ''
        items.append({
            'id': mid,
            'name': label,
            'description': f'Antigravity Hub 原生模型 ({mid}){quota_hint}',
            'isDefault': len(items) == 0,
            'defaultEffort': inferred,
            'efforts': _MAX_EFFORTS,
            'supportsFast': False,
            'supportsImage': bool(cfg.get('supportsImages')),
            'planModel': plan_enum,
        })
    return items


def resolve_plan_model(model_id: str | None, effort: str | None, client_configs: list[dict]) -> str:
    """Resolve `(model_id, effort)` to the Hub's internal `planModel` enum identifier."""
    live_map = {}
    for cfg in client_configs:
        mid = (cfg.get('modelId') or '').strip()
        plan_enum = (cfg.get('modelOrAlias') or {}).get('model')
        if mid and mid == model_id and cfg.get('disabled'):
            raise RuntimeError(cfg.get('tagDescription') or f'当前账号无法使用模型 {mid}')
        if mid and plan_enum and not cfg.get('disabled'):
            live_map[mid] = plan_enum
    merged = {**_BUILTIN_PLAN_MODELS, **live_map}

    target_id = (model_id or '').strip() or 'gemini-4-argon-high'
    if effort:
        tier = effort if effort in _MAX_EFFORTS else 'high'
        base = re.sub(r'-(low|medium|high|xhigh|max)$', '', target_id)
        candidate = f'{base}-{tier}'
        if candidate not in merged and tier in ('xhigh', 'max'):
            candidate = f'{base}-high'
        if candidate in merged:
            target_id = candidate

    if target_id in merged:
        return merged[target_id]
    if target_id.startswith('MODEL_'):
        return target_id
    return next(iter(live_map.values()), 'MODEL_PLACEHOLDER_M418')


def build_cascade_config(plan_model: str, mode: str) -> dict:
    if mode == 'plan':
        auto_policy = 'CASCADE_COMMANDS_AUTO_EXECUTION_OFF'
        planner_mode = 'CONVERSATIONAL_PLANNER_MODE_READ_ONLY'
    elif mode == 'auto':
        auto_policy = 'CASCADE_COMMANDS_AUTO_EXECUTION_EAGER'
        planner_mode = 'CONVERSATIONAL_PLANNER_MODE_DEFAULT'
    else:
        auto_policy = 'CASCADE_COMMANDS_AUTO_EXECUTION_PROCEED_IN_SANDBOX'
        planner_mode = 'CONVERSATIONAL_PLANNER_MODE_DEFAULT'

    return {
        'plannerConfig': {
            'conversational': {
                'plannerMode': planner_mode,
            },
            'supportsLatexRendering': True,
            'toolConfig': {
                'runCommand': {
                    'autoCommandConfig': {
                        'autoExecutionPolicy': auto_policy,
                    }
                },
                'notifyUser': {},
            },
            'planModel': plan_model,
            'useAiCredits': False,
        }
    }


def _default_agent_spec() -> dict:
    return {
        'builtinAgent': {
            'defaultAgent': {
                'isGoogle': False,
                'isInteractive': True,
                'surface': 'AGENT_SURFACE_HUB',
            }
        }
    }


async def probe_hub_roundtrip(hub: AntigravityHubClient) -> dict:
    """Verify real model generation via an ephemeral Hub cascade and clean it up immediately."""
    user = await hub.get_user_status()
    email = user.get('email') or user.get('name') or '本机账号'
    plan_name = ((user.get('planStatus') or {}).get('planInfo') or {}).get('planName') or 'Pro'
    configs = await hub.get_model_configs()
    plan_model = resolve_plan_model('gemini-4-argon-low', 'low', configs)

    cid = str(uuid.uuid4())
    folder = (db.DATA / 'diagnostics').resolve()
    folder.mkdir(exist_ok=True, mode=0o700)
    started = await hub.call('StartCascade', {
        'cascadeId': cid,
        'source': 'CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT',
        'workspaceUris': [folder.as_uri()],
    }, timeout=10.0)
    cid = started.get('cascadeId') or cid

    try:
        await hub.call('SendUserCascadeMessage', {
            'cascadeId': cid,
            'items': [{'text': 'Reply with ONLY WORKSTATION_ANTIGRAVITY_OK and do not call any tools.'}],
            'cascadeConfig': build_cascade_config(plan_model, 'plan'),
            'customAgentSpec': _default_agent_spec(),
        }, timeout=15.0)

        deadline = time.monotonic() + 28.0
        saw_running = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.45)
            trajs = (await hub.call('GetAllCascadeTrajectories', {}, timeout=8.0)).get('trajectorySummaries') or {}
            info = trajs.get(cid) or {}
            run_status = info.get('status') or ''
            if run_status in _ACTIVE_CASCADE_STATUSES:
                saw_running = True
            steps = (await hub.call('GetCascadeTrajectorySteps', {'cascadeId': cid, 'stepOffset': 0}, timeout=8.0)).get('steps') or []
            for st in steps:
                if st.get('type') == 'CORTEX_STEP_TYPE_ERROR_MESSAGE':
                    err = ((st.get('errorMessage') or {}).get('error') or {}).get('shortError') or 'Antigravity Hub 返回错误'
                    raise RuntimeError(err)
                pr = st.get('plannerResponse') or {}
                txt = (pr.get('modifiedResponse') or pr.get('response') or '').strip()
                if txt and st.get('status') == 'CORTEX_STEP_STATUS_DONE':
                    return {
                        'status': 'ready',
                        'code': None,
                        'detail': f'已连接本机 Antigravity Hub ({email} · {plan_name})，真实模型调用成功',
                    }
            if saw_running and run_status and run_status not in _ACTIVE_CASCADE_STATUSES and len(steps) >= 2:
                break
        raise RuntimeError('Antigravity Hub 模型响应超时，请确认本机网络状态。')
    finally:
        try:
            await hub.call('DeleteCascadeTrajectory', {'cascadeId': cid}, timeout=5.0)
        except Exception:
            pass


def _uri_to_path(uri_or_path: str) -> str:
    if not uri_or_path:
        return ''
    if uri_or_path.startswith('file://'):
        return unquote(urlparse(uri_or_path).path)
    return uri_or_path


def _extract_code_action_change(step: dict, workdir: str) -> list[dict]:
    ca = step.get('codeAction') or {}
    spec = ca.get('actionSpec') or {}
    path_val = ''
    kind_type = 'update'
    for key, mapped in (('createFile', 'add'), ('editFile', 'update'), ('replaceFile', 'update'), ('deleteFile', 'delete')):
        if key in spec and isinstance(spec[key], dict):
            raw = spec[key].get('path') or spec[key].get('absolutePathUri') or ''
            if isinstance(raw, dict):
                raw = raw.get('absolutePathUri') or raw.get('path') or ''
            path_val = _uri_to_path(str(raw))
            kind_type = mapped
            break
    if not path_val:
        tc = (step.get('metadata') or {}).get('toolCall') or {}
        try:
            args = json.loads(tc.get('argumentsJson') or '{}')
            path_val = _uri_to_path(str(args.get('Path') or args.get('TargetFile') or ''))
            if tc.get('name') == 'write_file':
                kind_type = 'add'
        except Exception:
            pass
    if not path_val:
        return []
    try:
        rel = str(Path(path_val).resolve().relative_to(Path(workdir).resolve()))
    except Exception:
        rel = path_val
    return [{'path': rel, 'kind': {'type': kind_type}}]


async def _handle_question_step(hub, cascade_id, trajectory_id, step_index, step, job, session):
    questions = (step.get('askQuestion') or {}).get('questions') or []
    if not questions:
        raise RuntimeError('Antigravity 选择请求缺少问题内容，请在原生客户端处理')
    params = {'questions': [
        {'id': str(index), 'question': q.get('question', ''), 'isMultiSelect': bool(q.get('isMultiSelect')),
         'options': [{'label': str(o.get('id', '')), 'description': o.get('text', '')}
                     for o in q.get('options', [])]}
        for index, q in enumerate(questions)
    ]}
    ident = os.urandom(12).hex()
    method = 'item/tool/requestUserInput'
    db.execute('INSERT INTO approvals VALUES(?,?,?,?,NULL,?)',
               (ident, job['id'], method, json.dumps(params, ensure_ascii=False), time.time()))
    db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (job['id'],))
    db.event(session['id'], job['id'], 'approval', {'id': ident, 'method': method, 'params': params})
    while True:
        state = db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))
        if state['stop']:
            return
        approval = db.one('SELECT decision FROM approvals WHERE id=?', (ident,))
        if approval['decision']:
            answers = json.loads(approval['decision'])['answers']
            break
        await asyncio.sleep(.3)
    entries = []
    for index, question in enumerate(questions):
        values = answers[str(index)]['answers']
        option_ids = {str(o.get('id', '')) for o in question.get('options', [])}
        entries.append({**question,
                        'selectedOptionIds': [v for v in values if v in option_ids],
                        'writeInResponse': '\n'.join(v for v in values if v not in option_ids),
                        'skipped': False})
    source = (step.get('metadata') or {}).get('sourceTrajectoryStepInfo') or {}
    await hub.call('HandleCascadeUserInteraction', {
        'cascadeId': cascade_id,
        'interaction': {'trajectoryId': source.get('trajectoryId') or trajectory_id,
                        'stepIndex': source.get('stepIndex', step_index),
                        'askQuestion': {'questions': entries}},
    }, timeout=10.0)
    db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='waiting'", (job['id'],))


async def _handle_waiting_step(hub: AntigravityHubClient, cascade_id: str, trajectory_id: str, step_index: int, step: dict, mode: str, job: dict, session: dict):
    """Resolve a `CORTEX_STEP_STATUS_WAITING` permission/command interaction automatically or via web approval."""
    if step.get('askQuestion') or (step.get('metadata') or {}).get('toolCall', {}).get('name') == 'ask_question':
        await _handle_question_step(hub, cascade_id, trajectory_id, step_index, step, job, session)
        return
    rc = step.get('runCommand') or {}
    proposed_cmd = rc.get('proposedCommandLine') or rc.get('commandLine') or ''
    cwd = rc.get('cwd') or ''
    tc = (step.get('metadata') or {}).get('toolCall') or {}
    reason = proposed_cmd or tc.get('name') or '允许 Antigravity 执行当前工具操作？'

    if mode == 'auto':
        allow = True
    else:
        ident = os.urandom(12).hex()
        params = {'command': proposed_cmd or None, 'reason': reason, 'cwd': cwd or None}
        db.execute(
            'INSERT INTO approvals VALUES(?,?,?,?,NULL,?)',
            (ident, job['id'], 'antigravity/permission', json.dumps(params, ensure_ascii=False), time.time()),
        )
        db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (job['id'],))
        db.event(session['id'], job['id'], 'approval', {'id': ident, 'method': 'antigravity/permission', 'params': params})
        from . import webpush
        proj = db.one('SELECT name FROM projects WHERE id=?', (session['project_id'],))
        await webpush.notify_job_approval(session, proj, ident, 'antigravity/permission', params)
        allow = False
        while True:
            approval = db.one('SELECT decision FROM approvals WHERE id=?', (ident,))
            state = db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))
            if approval['decision'] or state['stop']:
                pending = db.one('SELECT count(*) AS n FROM approvals WHERE job_id=? AND id!=? AND decision IS NULL', (job['id'], ident))['n']
                db.execute('UPDATE jobs SET status=? WHERE id=?', ('waiting' if pending else 'running', job['id']))
                allow = (approval['decision'] == 'accept') and not state['stop']
                break
            await asyncio.sleep(0.3)

    step_info = (step.get('metadata') or {}).get('sourceTrajectoryStepInfo') or {}
    traj_id = step_info.get('trajectoryId') or trajectory_id
    s_idx = step_info.get('stepIndex', step_index)

    interaction_payload = {
        'trajectoryId': traj_id,
        'stepIndex': s_idx,
        'permission': {
            'allow': allow,
            'scope': 'PERMISSION_SCOPE_ONCE',
        },
    }
    try:
        await hub.call('HandleCascadeUserInteraction', {
            'cascadeId': cascade_id,
            'interaction': interaction_payload,
        }, timeout=10.0)
    except Exception:
        if proposed_cmd:
            try:
                await hub.call('HandleCascadeUserInteraction', {
                    'cascadeId': cascade_id,
                    'interaction': {
                        'trajectoryId': traj_id,
                        'stepIndex': s_idx,
                        'runCommand': {
                            'confirm': allow,
                            'proposedCommandLine': proposed_cmd,
                            'submittedCommandLine': proposed_cmd,
                        },
                    },
                }, timeout=10.0)
            except Exception:
                pass


async def run_hub_turn(hub: AntigravityHubClient, job: dict, session: dict, project: dict, images: list[Path]) -> dict:
    """Execute a full conversational turn on the local Antigravity Hub and stream structured events."""
    mode = job.get('mode') or session.get('mode') or 'standard'
    model = job.get('model') or session.get('model') or None
    effort = job.get('effort') or session.get('effort') or None
    workdir = db.session_workdir(session, project)
    workspace_uri = Path(workdir).resolve().as_uri()

    configs = await hub.get_model_configs()
    plan_model = resolve_plan_model(model, effort, configs)

    handoff_ctx = context_bridge.build_handoff_context(
        session, project, target_engine='antigravity', exclude_job_id=job['id']
    )

    trajs = (await hub.call('GetAllCascadeTrajectories', {}, timeout=10.0)).get('trajectorySummaries') or {}
    cascade_id = db.get_antigravity_native_id(session)
    if not cascade_id or cascade_id not in trajs:
        new_cid = str(uuid.uuid4())
        started = await hub.call('StartCascade', {
            'cascadeId': new_cid,
            'source': 'CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT',
            'workspaceUris': [workspace_uri],
        }, timeout=12.0)
        cascade_id = started.get('cascadeId') or new_cid
        db.execute('UPDATE sessions SET native_id=?, native_antigravity_id=?, last_engine=?, cwd=COALESCE(cwd, ?) WHERE id=?', (cascade_id, cascade_id, 'antigravity', workdir, session['id']))
        db.event(session['id'], job['id'], 'native_session', {'id': cascade_id})
        start_offset = 0
    else:
        db.execute('UPDATE sessions SET native_id=?, native_antigravity_id=?, last_engine=?, cwd=COALESCE(cwd, ?) WHERE id=?', (cascade_id, cascade_id, 'antigravity', workdir, session['id']))
        existing_steps = (await hub.call('GetCascadeTrajectorySteps', {'cascadeId': cascade_id, 'stepOffset': 0}, timeout=10.0)).get('steps') or []
        start_offset = len(existing_steps)

    prompt_text = job['prompt']
    if handoff_ctx:
        prompt_text = f'{handoff_ctx}\n\n{prompt_text}'
    elif start_offset == 0:
        mem_ctx = memories.build_memory_context(project['id'])
        design_ctx = design_guard.get_design_directive(prompt_text=job['prompt'])
        prompt_parts = []
        if mem_ctx:
            prompt_parts.append(mem_ctx)
        if design_ctx:
            prompt_parts.append(design_ctx)
        prompt_parts.append(prompt_text)
        prompt_text = '\n\n'.join(prompt_parts)
    if mode == 'plan':
        prompt_text = f'[只读规划模式：请仅分析代码并输出清晰方案，不要修改、创建或删除任何文件]\n\n{prompt_text}'
    if images:
        refs = '\n'.join(f'- {p}' for p in images)
        prompt_text = f'{prompt_text}\n\n[附加本机图片文件（可直接用 read_file 查看）]\n{refs}'

    await hub.call('SendUserCascadeMessage', {
        'cascadeId': cascade_id,
        'items': [{'text': prompt_text}],
        'cascadeConfig': build_cascade_config(plan_model, mode),
        'customAgentSpec': _default_agent_spec(),
    }, timeout=20.0)

    emitted_snapshots: dict[str, str] = {}
    handled_waiting: set[int] = set()
    final_response = ''
    last_error = None
    saw_active_or_progress = False
    idle_polls_after_progress = 0

    def emit_step(step_id: str, payload: dict):
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if emitted_snapshots.get(step_id) == encoded:
            return
        emitted_snapshots[step_id] = encoded
        db.event(session['id'], job['id'], 'antigravity', {'event': 'agy_step', 'step_id': step_id, **payload})

    while True:
        if db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))['stop']:
            try:
                await hub.call('CancelCascadeInvocation', {'cascadeId': cascade_id}, timeout=8.0)
            except Exception:
                pass
            return {'status': 'stopped', 'error': None}

        await asyncio.sleep(0.35)
        trajs = (await hub.call('GetAllCascadeTrajectories', {}, timeout=10.0)).get('trajectorySummaries') or {}
        info = trajs.get(cascade_id) or {}
        run_status = info.get('status') or ''
        trajectory_id = info.get('trajectoryId') or ''

        steps_data = await hub.call('GetCascadeTrajectorySteps', {'cascadeId': cascade_id, 'stepOffset': start_offset}, timeout=12.0)
        new_steps = steps_data.get('steps') or []

        if run_status in _ACTIVE_CASCADE_STATUSES or len(new_steps) >= 2:
            saw_active_or_progress = True

        step_prefix = cascade_id or job['id']
        for rel_idx, st in enumerate(new_steps):
            abs_idx = start_offset + rel_idx
            stype = st.get('type') or ''
            sstatus = st.get('status') or ''
            done = sstatus in ('CORTEX_STEP_STATUS_DONE', 'CORTEX_STEP_STATUS_ERROR', 'CORTEX_STEP_STATUS_CANCELED')

            if sstatus == 'CORTEX_STEP_STATUS_WAITING' and abs_idx not in handled_waiting:
                handled_waiting.add(abs_idx)
                await _handle_waiting_step(hub, cascade_id, trajectory_id, abs_idx, st, mode, job, session)

            if stype == 'CORTEX_STEP_TYPE_PLANNER_RESPONSE':
                pr = st.get('plannerResponse') or {}
                thinking = (pr.get('thinking') or '').strip()
                if thinking:
                    emit_step(f'agy-think-{step_prefix}-{abs_idx}', {
                        'step_kind': 'reasoning',
                        'text': thinking,
                        'completed': done,
                    })
                resp_text = pr.get('modifiedResponse') if pr.get('modifiedResponse') is not None else (pr.get('response') or '')
                if resp_text:
                    final_response = resp_text
                    emit_step(f'agy-msg-{step_prefix}-{abs_idx}', {
                        'step_kind': 'agent',
                        'text': resp_text,
                        'completed': done,
                    })

            elif stype == 'CORTEX_STEP_TYPE_RUN_COMMAND':
                rc = st.get('runCommand') or {}
                cmd = rc.get('commandLine') or rc.get('proposedCommandLine') or 'shell'
                comb = rc.get('combinedOutput')
                out_text = comb.get('full', '') if isinstance(comb, dict) else (str(comb) if comb else '')
                emit_step(f'agy-cmd-{step_prefix}-{abs_idx}', {
                    'step_kind': 'command',
                    'command': cmd,
                    'cwd': rc.get('cwd') or workdir,
                    'exitCode': rc.get('exitCode'),
                    'output': out_text[:16000],
                    'completed': done,
                })

            elif stype == 'CORTEX_STEP_TYPE_CODE_ACTION':
                changes = _extract_code_action_change(st, workdir)
                if changes:
                    emit_step(f'agy-chg-{step_prefix}-{abs_idx}', {
                        'step_kind': 'change',
                        'changes': changes,
                        'completed': done,
                    })

            elif stype == 'CORTEX_STEP_TYPE_VIEW_FILE':
                vf = st.get('viewFile') or {}
                p_str = _uri_to_path(vf.get('absolutePathUri') or '')
                short_name = Path(p_str).name if p_str else '文件'
                emit_step(f'agy-view-{step_prefix}-{abs_idx}', {
                    'step_kind': 'tool',
                    'tool_kind': 'read',
                    'title': f'读取文件：{short_name}',
                    'detail': p_str,
                    'completed': done,
                })

            elif stype == 'CORTEX_STEP_TYPE_GENERIC':
                tc = (st.get('metadata') or {}).get('toolCall') or {}
                tname = tc.get('name') or 'tool'
                tool_kind = 'search' if 'search' in tname else 'mcp'
                emit_step(f'agy-tool-{step_prefix}-{abs_idx}', {
                    'step_kind': 'tool',
                    'tool_kind': tool_kind,
                    'title': f'联网搜索' if 'search_web' in tname else f'调用工具：{tname}',
                    'detail': (tc.get('argumentsJson') or '')[:1200],
                    'completed': done,
                })

            elif stype == 'CORTEX_STEP_TYPE_ERROR_MESSAGE':
                err_obj = ((st.get('errorMessage') or {}).get('error') or {})
                last_error = err_obj.get('userErrorMessage') or err_obj.get('shortError') or 'Antigravity 执行出错'

        all_steps_settled = bool(new_steps) and all(
            (s.get('status') or '') not in ('CORTEX_STEP_STATUS_RUNNING', 'CORTEX_STEP_STATUS_GENERATING', 'CORTEX_STEP_STATUS_PENDING', 'CORTEX_STEP_STATUS_QUEUED', 'CORTEX_STEP_STATUS_WAITING')
            for s in new_steps
        )
        if saw_active_or_progress and run_status not in _ACTIVE_CASCADE_STATUSES and all_steps_settled:
            idle_polls_after_progress += 1
            if idle_polls_after_progress >= 2:
                break
        else:
            idle_polls_after_progress = 0

    if last_error and not final_response:
        # Generic cascade failures can hide an expired desktop login.
        try:
            await hub.retrieve_user_quota_summary(force_refresh=True)
        except Exception as exc:
            detail = str(exc)
            if 'not logged into' in detail.lower() or 'not authenticated' in detail.lower():
                last_error = 'Antigravity authentication required: not logged into Antigravity'
        return {'status': 'failed', 'error': last_error}

    db.event(session['id'], job['id'], 'antigravity', {
        'event': 'result',
        'has_structured_steps': bool(emitted_snapshots),
        'result': {
            'conversation_id': cascade_id,
            'status': 'SUCCESS',
            'response': final_response,
        },
    })
    return {'status': 'completed', 'error': None}
