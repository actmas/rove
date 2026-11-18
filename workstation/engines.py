"""User-triggered engine diagnostics and dynamic model/effort catalog discovery."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import time

from . import agents
from . import antigravity_hub
from . import store as db
from .rpc import CodexRPC

EFFORT_LABELS = {
    'low': {'label': '极速 (Low)', 'short': 'Low', 'description': '轻量推理，响应最快'},
    'medium': {'label': '均衡 (Medium)', 'short': 'Medium', 'description': '兼顾速度与思考深度，适合日常编码'},
    'high': {'label': '深入 (High)', 'short': 'High', 'description': '深度推理，适合复杂架构与排错'},
    'xhigh': {'label': '高强 (X-High)', 'short': 'X-High', 'description': '超高强度推理，攻克棘手逻辑'},
    'max': {'label': '极限 (Max)', 'short': 'Max', 'description': '最大思考预算，处理最高难度问题'},
    'ultra': {'label': '超算 (Ultra)', 'short': 'Ultra', 'description': '极限推理并支持自动子任务协同'},
}

EXECUTION_MODES = {
    'codex': [
        {'id': 'standard', 'label': '标准审批', 'short': '标准', 'description': '允许工作区读写与网络，敏感命令需手动批准'},
        {'id': 'full', 'label': '完全访问', 'short': '完全', 'description': '放行系统命令与网络（无沙箱限制），支持 SSH、跨目录与系统运维'},
        {'id': 'auto', 'label': '自动执行', 'short': '自动', 'description': '自动审批并放行网络，免等待手动确认'},
        {'id': 'plan', 'label': '只读规划', 'short': '规划', 'description': '仅读取与分析代码、输出方案，不修改任何文件'},
    ],
    'antigravity': [
        {'id': 'standard', 'label': '接受修改', 'short': '标准', 'description': '默认接受文件编辑（--mode accept-edits）'},
        {'id': 'full', 'label': '完全访问', 'short': '完全', 'description': '自动执行模式，完全放行'},
        {'id': 'auto', 'label': '自动执行', 'short': '自动', 'description': '自动执行模式（--mode auto）'},
        {'id': 'plan', 'label': '只读规划', 'short': '规划', 'description': '仅规划方案不执行修改（--mode plan）'},
    ],
}

_FULL_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
_MAX_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']

DEFAULT_MODELS = {
    'codex': [
        {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol', 'description': '最新主力编程模型，兼顾速度与高质量代码产出', 'isDefault': True,
         'defaultEffort': 'medium', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-6-astra', 'name': 'GPT-6-Astra', 'description': '旗舰前沿智力，适合最复杂的架构设计与疑难排查', 'isDefault': False,
         'defaultEffort': 'high', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-6.1-terra', 'name': 'GPT-6.1 Terra', 'description': 'GPT-6.1 Terra 专项编程模型', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-6-sol', 'name': 'GPT-6-Sol', 'description': '稳健均衡的 GPT-6 编码模型', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-6-luna', 'name': 'GPT-6-Luna', 'description': '轻快敏捷模型，适合快速问答与小幅改动', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-5.6-sol', 'name': 'GPT-5.6-Sol', 'description': '经典 GPT-5.6 主力模型', 'isDefault': False,
         'defaultEffort': 'low', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-5.6-terra', 'name': 'GPT-5.6-Terra', 'description': 'GPT-5.6 Terra 工程模型', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _FULL_EFFORTS, 'supportsFast': True, 'supportsImage': True},
        {'id': 'gpt-5.6-luna', 'name': 'GPT-5.6-Luna', 'description': 'GPT-5.6 轻量响应模型', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': True, 'supportsImage': True},
    ],
    'antigravity': [
        {'id': 'gemini-4-argon-high', 'name': 'Gemini 4 Argon (High)', 'description': 'Google 旗舰推理与代码架构模型（高思考）', 'isDefault': True,
         'defaultEffort': 'high', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gemini-4-argon-medium', 'name': 'Gemini 4 Argon (Medium)', 'description': 'Google 旗舰主力模型（均衡思考）', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gemini-4-argon-low', 'name': 'Gemini 4 Argon (Low)', 'description': 'Google 旗舰快速模型（轻量思考）', 'isDefault': False,
         'defaultEffort': 'low', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gemini-3.8-flash-high', 'name': 'Gemini 3.8 Flash (High)', 'description': '高速 Flash 模型（深度思考）', 'isDefault': False,
         'defaultEffort': 'high', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gemini-3.8-flash-medium', 'name': 'Gemini 3.8 Flash (Medium)', 'description': '高速 Flash 模型（标准思考）', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gemini-3.8-flash-low', 'name': 'Gemini 3.8 Flash (Low)', 'description': '极速 Flash 模型', 'isDefault': False,
         'defaultEffort': 'low', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-opus-5-5-high', 'name': 'Claude Opus 5.5 (High)', 'description': 'Claude Opus 5.5 深度推理', 'isDefault': False,
         'defaultEffort': 'high', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-opus-5-5-medium', 'name': 'Claude Opus 5.5 (Medium)', 'description': 'Claude Opus 5.5 均衡模式', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-opus-5-5-low', 'name': 'Claude Opus 5.5 (Low)', 'description': 'Claude Opus 5.5 轻量推理', 'isDefault': False,
         'defaultEffort': 'low', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-sonnet-5-5-high', 'name': 'Claude Sonnet 5.5 (High)', 'description': 'Claude Sonnet 5.5 深度编码', 'isDefault': False,
         'defaultEffort': 'high', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-sonnet-5-5-medium', 'name': 'Claude Sonnet 5.5 (Medium)', 'description': 'Claude Sonnet 5.5 日常编码', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'claude-sonnet-5-5-low', 'name': 'Claude Sonnet 5.5 (Low)', 'description': 'Claude Sonnet 5.5 快速响应', 'isDefault': False,
         'defaultEffort': 'low', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': True},
        {'id': 'gpt-oss-120b-medium', 'name': 'GPT-OSS 120B (Medium)', 'description': '开源 120B 权重模型', 'isDefault': False,
         'defaultEffort': 'medium', 'efforts': _MAX_EFFORTS, 'supportsFast': False, 'supportsImage': False},
    ],
}


def antigravity_cli_env() -> dict[str, str]:
    """Build subprocess environment for standalone `agy` CLI, inheriting configured or macOS system proxy."""
    env = os.environ.copy()
    proxy = (db.config().get('antigravity_proxy') or '').strip()
    if not proxy and not (env.get('HTTPS_PROXY') or env.get('https_proxy') or env.get('ALL_PROXY')):
        try:
            raw = subprocess.check_output(['scutil', '--proxy'], text=True, timeout=2)
            vals = dict(re.findall(r'^\s*([A-Za-z0-9]+)\s*:\s*(\S+)', raw, flags=re.M))
            if vals.get('HTTPSEnable') == '1' and vals.get('HTTPSProxy') and vals.get('HTTPSPort'):
                proxy = f"http://{vals['HTTPSProxy']}:{vals['HTTPSPort']}"
            elif vals.get('HTTPEnable') == '1' and vals.get('HTTPProxy') and vals.get('HTTPPort'):
                proxy = f"http://{vals['HTTPProxy']}:{vals['HTTPPort']}"
            elif vals.get('SOCKSEnable') == '1' and vals.get('SOCKSProxy') and vals.get('SOCKSPort'):
                proxy = f"socks5://{vals['SOCKSProxy']}:{vals['SOCKSPort']}"
        except Exception:
            proxy = ''
    if proxy:
        env.setdefault('HTTPS_PROXY', proxy)
        env.setdefault('HTTP_PROXY', proxy)
        env.setdefault('ALL_PROXY', proxy)
        env.setdefault('NO_PROXY', '127.0.0.1,localhost')
    return env


def classify_error(message):
    text = (message or '').lower()
    if 'not currently available in your location' in text or 'location is not supported' in text:
        return {'status': 'blocked', 'code': 'region_restricted',
                'detail': '独立 CLI 登录未通过 Google 地区资格校验。请保持本机 Gemini.app / Antigravity 桌面端开启（工作台将自动复用其已授权的本机 Hub 通道），或核对账号关联地区与网络出口。',
                'help_url': 'https://antigravity.google/docs/faq'}
    if 'verify your account' in text or 'verification required' in text:
        return {'status': 'blocked', 'code': 'verification_required',
                'detail': 'Google 要求进一步验证此账号。请在官方原生客户端完成验证，再重新检查连接。',
                'help_url': 'https://antigravity.google/docs/install'}
    if 'authentication' in text or 'not authenticated' in text or 'sign in' in text or 'not logged into' in text:
        return {'status': 'unavailable', 'code': 'authentication_required',
                'detail': 'Antigravity 需要本机登录。请运行项目中的“登录 Antigravity.command”。',
                'help_url': 'https://antigravity.google/docs/install'}
    return {'status': 'unavailable', 'code': 'connection_failed',
            'detail': (message or '工具未成功完成连接检查')[:2000],
            'help_url': 'https://antigravity.google/docs/cli/troubleshooting/'}


def record(engine, value):
    value = {**value, 'checked_at': time.time()}
    db.execute('INSERT INTO engine_checks(engine,payload) VALUES(?,?) ON CONFLICT(engine) DO UPDATE SET payload=excluded.payload',
               (engine, json.dumps(value, ensure_ascii=False)))
    return value


def state(engine):
    row = db.one('SELECT payload FROM engine_checks WHERE engine=?', (engine,))
    return json.loads(row['payload']) if row else {'status': 'unchecked', 'code': None, 'detail': '尚未检查连接', 'checked_at': None}


def save_models(engine, models):
    now = time.time()
    db.execute('INSERT INTO engine_models(engine,payload,updated) VALUES(?,?,?) ON CONFLICT(engine) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',
               (engine, json.dumps(models, ensure_ascii=False), now))
    return {'models': models, 'updated': now, 'source': 'live'}


def engine_models(engine):
    row = db.one('SELECT payload,updated FROM engine_models WHERE engine=?', (engine,))
    if row:
        try:
            parsed = json.loads(row['payload'])
            if parsed:
                return {'models': parsed, 'updated': row['updated'], 'source': 'live'}
        except ValueError:
            pass
    if engine in DEFAULT_MODELS:
        return {'models': DEFAULT_MODELS[engine], 'updated': None, 'source': 'builtin'}
    spec = agents.get_agent_spec(engine)
    if spec and spec.get('models'):
        return {'models': spec['models'], 'updated': spec.get('updated'), 'source': 'custom'}
    return {'models': [], 'updated': None, 'source': 'builtin'}


def catalog():
    custom_agents = agents.load_custom_agents()
    modes = dict(EXECUTION_MODES)
    result = {
        'codex': engine_models('codex'),
        'antigravity': engine_models('antigravity'),
    }
    for agent_id, spec in custom_agents.items():
        if not spec.get('enabled', True):
            continue
        result[agent_id] = engine_models(agent_id)
        modes[agent_id] = spec.get('modes') or EXECUTION_MODES['antigravity']
    result['efforts'] = EFFORT_LABELS
    result['modes'] = modes
    result['agents'] = agents.list_all_agents()
    return result


def resolve_model(engine, model, effort=None, allow_auto: bool = False):
    """Return (model, effort) that is valid for ``engine``.

    A model id that belongs to another engine's catalog (e.g. a GPT model left on an Antigravity
    or custom agent session after a relay switch) is replaced by the engine default.
    Unknown/custom ids are preserved. When ``allow_auto`` is True, explicit ``'auto'`` preferences
    for model or effort (or engine='auto') are preserved.
    """
    if engine == 'auto':
        if allow_auto:
            return (model or 'auto'), (effort or 'auto')
        engine = 'codex'
    own = engine_models(engine)['models']
    own_ids = {m.get('id') for m in own}
    default = next((m for m in own if m.get('isDefault')), own[0] if own else None)
    if model == 'auto':
        if allow_auto:
            next_effort = 'auto' if effort == 'auto' else effort
            return 'auto', next_effort
        if not default:
            return None, ('medium' if effort == 'auto' else effort)
        efforts = default.get('efforts') or []
        next_effort = effort if (effort != 'auto' and effort in efforts) else (default.get('defaultEffort') or 'medium')
        return default.get('id'), next_effort

    all_engines = ['codex', 'antigravity'] + list(agents.load_custom_agents().keys())
    other_ids = set()
    for other_eng in all_engines:
        if other_eng == engine:
            continue
        for m in engine_models(other_eng)['models']:
            if m.get('id'):
                other_ids.add(m['id'])
    other_ids -= own_ids
    if model and (model in own_ids or model not in other_ids):
        if effort == 'auto' and not allow_auto:
            matched = next((m for m in own if m.get('id') == model), default)
            fallback_eff = (matched or {}).get('defaultEffort') or 'medium'
            return model, fallback_eff
        return model, effort
    if not default:
        return None, ('medium' if (effort == 'auto' and not allow_auto) else effort)
    efforts = default.get('efforts') or []
    if effort == 'auto' and allow_auto:
        next_effort = 'auto'
    else:
        next_effort = effort if (effort != 'auto' and effort in efforts) else (default.get('defaultEffort') or effort)
    return default.get('id'), next_effort


async def discover_codex_models():
    binary = db.config().get('codex_bin', '')
    if not Path(binary).is_file():
        return engine_models('codex')
    async def noop(*_args):
        pass
    rpc = CodexRPC(binary, noop, noop)
    try:
        await asyncio.wait_for(rpc.start(), 15)
        res = await rpc.call('model/list', {}, timeout=15)
        items = []
        for m in res.get('data', []):
            if m.get('hidden'):
                continue
            efforts = [e['reasoningEffort'] for e in m.get('supportedReasoningEfforts', []) if e.get('reasoningEffort')] or ['low', 'medium', 'high']
            default_effort = m.get('defaultReasoningEffort') or ('medium' if 'medium' in efforts else efforts[0])
            tiers = [t.get('id') for t in m.get('serviceTiers', [])]
            modalities = m.get('inputModalities') or ['text']
            items.append({
                'id': m['id'],
                'name': m.get('displayName') or m['id'],
                'description': m.get('description') or '',
                'isDefault': bool(m.get('isDefault')),
                'defaultEffort': default_effort,
                'efforts': efforts,
                'supportsFast': 'priority' in tiers or 'fast' in (m.get('additionalSpeedTiers') or []),
                'supportsImage': 'image' in modalities,
            })
        if items:
            return save_models('codex', items)
    except Exception:
        pass
    finally:
        await rpc.close()
    return engine_models('codex')


async def discover_antigravity_models():
    hub = await antigravity_hub.discover_hub(auto_launch=False)
    if hub is not None:
        try:
            user = await hub.get_user_status()
            configs = await hub.get_model_configs()
            items = antigravity_hub.format_hub_models(configs)
            current = state('antigravity')
            if current.get('code') == 'region_restricted' and (user.get('email') or user.get('planStatus')):
                email = user.get('email') or user.get('name') or '本机账号'
                plan_name = ((user.get('planStatus') or {}).get('planInfo') or {}).get('planName') or 'Pro'
                record('antigravity', {
                    'status': 'ready',
                    'code': None,
                    'detail': f'已自动切换至本机 Antigravity Hub ({email} · {plan_name})',
                })
            if items:
                return save_models('antigravity', items)
        except Exception:
            pass

    binary = db.config().get('antigravity_bin', '')
    if not Path(binary).is_file():
        return engine_models('antigravity')
    proc = await asyncio.create_subprocess_exec(
        binary, 'models',
        env=antigravity_cli_env(),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        if proc.returncode == 0:
            items = []
            for idx, line in enumerate(out.decode(errors='replace').splitlines()):
                line = line.strip()
                if not line or '\t' not in line:
                    continue
                mid, name = [part.strip() for part in line.split('\t', 1)]
                inferred = 'high' if mid.endswith('-high') else 'low' if mid.endswith('-low') else 'medium'
                items.append({
                    'id': mid,
                    'name': name or mid,
                    'description': f'Antigravity 原生模型 ({mid})',
                    'isDefault': idx == 0,
                    'defaultEffort': inferred,
                    'efforts': _MAX_EFFORTS,
                    'supportsFast': False,
                    'supportsImage': not mid.startswith('gpt-oss'),
                })
            if items:
                return save_models('antigravity', items)
    except Exception:
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return engine_models('antigravity')


async def refresh_models():
    await asyncio.gather(discover_codex_models(), discover_antigravity_models())
    return catalog()


async def check_antigravity():
    hub = await antigravity_hub.discover_hub(auto_launch=True)
    if hub is not None:
        try:
            outcome = await antigravity_hub.probe_hub_roundtrip(hub)
            return record('antigravity', outcome)
        except Exception as exc:
            return record('antigravity', classify_error(str(exc)))

    binary = db.config()['antigravity_bin']
    if not Path(binary).is_file():
        return record('antigravity', {'status': 'unavailable', 'code': 'not_installed', 'detail': 'Antigravity CLI 与桌面端均未安装'})
    folder = db.DATA / 'diagnostics'
    folder.mkdir(exist_ok=True, mode=0o700)
    proc = await asyncio.create_subprocess_exec(binary, '-p', 'Reply exactly WORKSTATION_ANTIGRAVITY_OK. Do not use tools or access files.',
                                              '--output-format', 'stream-json', '--print-timeout', '25s', cwd=folder,
                                              env=antigravity_cli_env(),
                                              stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 35)
        result = None
        for line in out.decode(errors='replace').splitlines():
            try:
                payload = json.loads(line)
                if payload.get('event') == 'result':
                    result = payload.get('result', payload)
            except ValueError:
                pass
        if proc.returncode == 0 and result and result.get('status') == 'SUCCESS':
            return record('antigravity', {'status': 'ready', 'code': None, 'detail': '真实模型调用成功'})
        error = (result or {}).get('error') or err.decode(errors='replace')[-2000:]
        return record('antigravity', classify_error(error))
    except asyncio.TimeoutError:
        return record('antigravity', {'status': 'unavailable', 'code': 'timeout', 'detail': '连接检查超时。请检查网络和本机登录状态。'})
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
