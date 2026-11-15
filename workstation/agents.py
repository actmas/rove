"""Universal pluggable Agent registry and execution adapter.

Allows Rove Workstation to dynamically register, switch, and collaborate across
arbitrary CLI or HTTP/OpenAI-compatible coding agents (e.g. Claude Code, Gemini CLI,
Aider, OpenCode, DeepSeek/Ollama/OpenRouter HTTP APIs, or custom scripts) alongside
the built-in Codex and Antigravity engines.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import context_bridge
from . import design_guard
from . import memories

def validate_agent_base_url(url: str | None) -> str:
    cleaned = (url or '').strip().rstrip('/')
    if not cleaned:
        raise ValueError('OpenAI 兼容 API Agent 必须填写 base_url（如 https://api.deepseek.com/v1）')
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError(f'非法的 base_url: {url}，必须是以 http:// 或 https:// 开头的合法 URL')
    return cleaned
from . import store as db

AGENT_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,39}$')
BUILTIN_ENGINES = ('codex', 'antigravity')

_DEFAULT_EFFORTS = ['low', 'medium', 'high']
_DEFAULT_MODES = [
    {'id': 'standard', 'label': '标准执行', 'short': '标准', 'description': '标准工作区读写与工具调用'},
    {'id': 'full', 'label': '完全访问', 'short': '完全', 'description': '放行全部工具与命令权限'},
    {'id': 'auto', 'label': '自动执行', 'short': '自动', 'description': '自动批准文件修改与命令执行'},
    {'id': 'plan', 'label': '只读规划', 'short': '规划', 'description': '仅分析架构与输出方案，不修改文件'},
]

AGENT_PRESETS: List[Dict[str, Any]] = [
    {
        'id': 'claude-code',
        'name': 'Claude Code',
        'short_name': 'Claude',
        'description': 'Anthropic 官方终端编程 Agent，擅长复杂代码重构与多文件协同',
        'adapter': 'cli',
        'binary': 'claude',
        'args_template': ['-p', '{prompt}', '--output-format', 'stream-json', '--verbose'],
        'model_flag': '--model',
        'resume_flag': '--resume',
        'mode_flags': {
            'standard': ['--permission-mode', 'acceptEdits'],
            'auto': ['--dangerously-skip-permissions'],
            'full': ['--dangerously-skip-permissions'],
            'plan': ['--permission-mode', 'plan'],
        },
        'output_format': 'claude-stream-json',
        'tone': 'orange',
        'models': [
            {
                'id': 'claude-sonnet-4-6',
                'name': 'Claude Sonnet 4.6',
                'description': 'Anthropic 主力高速高智商编码模型',
                'isDefault': True,
                'defaultEffort': 'high',
                'efforts': ['low', 'medium', 'high', 'max'],
                'supportsFast': False,
                'supportsImage': True,
            },
            {
                'id': 'claude-opus-4-6',
                'name': 'Claude Opus 4.6',
                'description': 'Anthropic 旗舰深度架构推理模型',
                'isDefault': False,
                'defaultEffort': 'high',
                'efforts': ['low', 'medium', 'high', 'max'],
                'supportsFast': False,
                'supportsImage': True,
            },
            {
                'id': 'claude-haiku-4-5',
                'name': 'Claude Haiku 4.5',
                'description': '轻量极速代码问答模型',
                'isDefault': False,
                'defaultEffort': 'low',
                'efforts': ['low', 'medium', 'high'],
                'supportsFast': False,
                'supportsImage': True,
            },
        ],
    },
    {
        'id': 'gemini-cli',
        'name': 'Gemini CLI',
        'short_name': 'Gemini',
        'description': 'Google 官方开源 Gemini 终端编程 Agent',
        'adapter': 'cli',
        'binary': 'gemini',
        'args_template': ['-p', '{prompt}'],
        'model_flag': '-m',
        'resume_flag': '',
        'mode_flags': {
            'standard': [],
            'auto': ['--yolo'],
            'full': ['--yolo'],
            'plan': [],
        },
        'output_format': 'text',
        'tone': 'blue',
        'models': [
            {
                'id': 'gemini-2.5-pro',
                'name': 'Gemini 2.5 Pro',
                'description': 'Gemini 2.5 Pro 超长上下文推理模型',
                'isDefault': True,
                'defaultEffort': 'high',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            },
            {
                'id': 'gemini-2.5-flash',
                'name': 'Gemini 2.5 Flash',
                'description': 'Gemini 2.5 Flash 高速响应模型',
                'isDefault': False,
                'defaultEffort': 'medium',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            },
        ],
    },
    {
        'id': 'aider',
        'name': 'Aider',
        'short_name': 'Aider',
        'description': '经典开源 Git 结对编程 CLI Agent，支持任意主流大模型',
        'adapter': 'cli',
        'binary': 'aider',
        'args_template': ['--message', '{prompt}', '--yes-always', '--no-pretty', '--no-stream'],
        'model_flag': '--model',
        'resume_flag': '',
        'mode_flags': {
            'standard': ['--yes-always'],
            'auto': ['--yes-always'],
            'full': ['--yes-always'],
            'plan': ['--architect', '--no-auto-commits'],
        },
        'output_format': 'text',
        'tone': 'teal',
        'models': [
            {
                'id': 'deepseek/deepseek-chat',
                'name': 'DeepSeek V3 (Aider)',
                'description': 'DeepSeek V3 编码模型',
                'isDefault': True,
                'defaultEffort': 'medium',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': False,
            },
            {
                'id': 'anthropic/claude-sonnet-4-20250514',
                'name': 'Claude Sonnet 4 (Aider)',
                'description': 'Anthropic Claude Sonnet 4',
                'isDefault': False,
                'defaultEffort': 'high',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            },
        ],
    },
    {
        'id': 'opencode',
        'name': 'OpenCode',
        'short_name': 'OpenCode',
        'description': '开源终端编程 Agent (opencode run)',
        'adapter': 'cli',
        'binary': 'opencode',
        'args_template': ['run', '{prompt}'],
        'model_flag': '--model',
        'resume_flag': '--session',
        'mode_flags': {
            'standard': [],
            'auto': [],
            'full': [],
            'plan': [],
        },
        'output_format': 'text',
        'tone': 'indigo',
        'models': [
            {
                'id': 'anthropic/claude-sonnet-4',
                'name': 'Claude Sonnet 4 (OpenCode)',
                'description': 'OpenCode 默认主力模型',
                'isDefault': True,
                'defaultEffort': 'high',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            },
            {
                'id': 'openai/gpt-4.1',
                'name': 'GPT-4.1 (OpenCode)',
                'description': 'OpenAI GPT-4.1 编程模型',
                'isDefault': False,
                'defaultEffort': 'medium',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            },
        ],
    },
    {
        'id': 'openai-http',
        'name': 'OpenAI 兼容 API',
        'short_name': 'API',
        'description': '通用 OpenAI 兼容接口（支持 DeepSeek、OpenRouter、Ollama、vLLM、硅基流动等）',
        'adapter': 'openai_http',
        'base_url': 'https://api.deepseek.com/v1',
        'api_key': '',
        'api_key_env': 'DEEPSEEK_API_KEY',
        'tone': 'cyan',
        'models': [
            {
                'id': 'deepseek-chat',
                'name': 'DeepSeek V3.2 Chat',
                'description': 'DeepSeek 高性价比主力代码与对话模型',
                'isDefault': True,
                'defaultEffort': 'medium',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': False,
            },
            {
                'id': 'deepseek-reasoner',
                'name': 'DeepSeek R1 Reasoner',
                'description': 'DeepSeek 深度推理模型（支持思维链流式输出）',
                'isDefault': False,
                'defaultEffort': 'high',
                'efforts': ['medium', 'high', 'max'],
                'supportsFast': False,
                'supportsImage': False,
            },
        ],
    },
]


def _agents_file() -> Path:
    return db.DATA / 'agents.json'


def load_custom_agents() -> Dict[str, Dict[str, Any]]:
    path = _agents_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            return {
                k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, dict) and k not in BUILTIN_ENGINES
            }
    except Exception:
        pass
    return {}


def save_custom_agents(agents_map: Dict[str, Dict[str, Any]]) -> None:
    clean = {
        k: v for k, v in agents_map.items()
        if isinstance(k, str) and isinstance(v, dict) and k not in BUILTIN_ENGINES
    }
    db.write_private(_agents_file(), json.dumps(clean, ensure_ascii=False, indent=2))


def list_presets() -> List[Dict[str, Any]]:
    out = []
    for preset in AGENT_PRESETS:
        item = dict(preset)
        if item.get('adapter') == 'cli':
            resolved = resolve_binary(item.get('binary', ''))
            item['detected_binary'] = resolved
        out.append(item)
    return out


def resolve_binary(binary_spec: str) -> Optional[str]:
    """Locate an executable on either macOS or Linux/PVE."""
    spec = (binary_spec or '').strip()
    if not spec:
        return None
    expanded = Path(spec).expanduser()
    if '/' in spec:
        return str(expanded) if expanded.is_file() and os.access(expanded, os.X_OK) else None

    search_dirs = [
        str(Path.home() / '.local/bin'),
        str(Path.home() / '.npm-global/bin'),
        str(Path.home() / '.cargo/bin'),
        '/opt/homebrew/bin',
        '/usr/local/bin',
        '/usr/bin',
        '/bin',
    ]
    path_env = os.environ.get('PATH', '')
    combined_path = ':'.join(search_dirs + ([path_env] if path_env else []))
    found = shutil.which(spec, path=combined_path)
    return found


def is_valid_engine(engine_id: Optional[str]) -> bool:
    if not engine_id or not isinstance(engine_id, str):
        return False
    if engine_id in BUILTIN_ENGINES:
        return True
    custom = load_custom_agents()
    spec = custom.get(engine_id)
    return bool(spec and spec.get('enabled', True))


def get_agent_spec(agent_id: str) -> Optional[Dict[str, Any]]:
    if agent_id in BUILTIN_ENGINES:
        cfg = db.config()
        cloud_url = cfg.get(f'{agent_id}_cloud_url') or ('https://chatgpt.com/codex' if agent_id == 'codex' else '')
        has_cloud = (agent_id == 'codex') or bool(cloud_url)
        return {
            'id': agent_id,
            'name': 'OpenAI Codex' if agent_id == 'codex' else 'Google Antigravity',
            'short_name': 'Codex' if agent_id == 'codex' else 'AGY',
            'builtin': True,
            'adapter': 'builtin',
            'enabled': True,
            'cloud_url': cloud_url,
            'execution_targets': ['local', 'cloud'] if has_cloud else ['local'],
        }
    custom = load_custom_agents().get(agent_id)
    if custom:
        return sanitize_agent_for_client(custom)
    return None


def is_agent_installed(agent_id: str) -> bool:
    if agent_id in BUILTIN_ENGINES:
        cfg = db.config()
        bin_path = cfg.get(f'{agent_id}_bin', '')
        return bool(bin_path and Path(bin_path).is_file())
    spec = get_agent_spec(agent_id)
    if not spec or not spec.get('enabled', True):
        return False
    adapter = spec.get('adapter', 'cli')
    if adapter == 'openai_http':
        return bool((spec.get('base_url') or '').strip())
    return bool(resolve_binary(spec.get('binary', '')))


def _normalize_models(raw_models: Any, agent_name: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_models, list) or not raw_models:
        return [{
            'id': 'default',
            'name': f'{agent_name} Default',
            'description': f'{agent_name} 默认模型',
            'isDefault': True,
            'defaultEffort': 'medium',
            'efforts': _DEFAULT_EFFORTS,
            'supportsFast': False,
            'supportsImage': True,
        }]
    normalized = []
    has_default = False
    for idx, m in enumerate(raw_models):
        if isinstance(m, str):
            mid = m.strip()
            if not mid:
                continue
            is_def = idx == 0 and not has_default
            has_default = has_default or is_def
            normalized.append({
                'id': mid,
                'name': mid,
                'description': f'{agent_name} 模型 ({mid})',
                'isDefault': is_def,
                'defaultEffort': 'medium',
                'efforts': _DEFAULT_EFFORTS,
                'supportsFast': False,
                'supportsImage': True,
            })
        elif isinstance(m, dict) and m.get('id'):
            mid = str(m['id']).strip()
            is_def = bool(m.get('isDefault'))
            if is_def:
                has_default = True
            efforts = m.get('efforts') if isinstance(m.get('efforts'), list) and m.get('efforts') else _DEFAULT_EFFORTS
            def_ef = m.get('defaultEffort') if m.get('defaultEffort') in efforts else efforts[min(1, len(efforts) - 1)]
            normalized.append({
                'id': mid,
                'name': str(m.get('name') or mid).strip(),
                'description': str(m.get('description') or f'{agent_name} 模型').strip(),
                'isDefault': is_def,
                'defaultEffort': def_ef,
                'efforts': efforts,
                'supportsFast': bool(m.get('supportsFast', False)),
                'supportsImage': bool(m.get('supportsImage', True)),
            })
    if normalized and not has_default:
        normalized[0]['isDefault'] = True
    return normalized


def upsert_custom_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    agent_id = str(payload.get('id') or '').strip().lower()
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError('Agent ID 仅支持小写字母、数字、连字符或下划线（2~40 位）')
    if agent_id in BUILTIN_ENGINES:
        raise ValueError(f'{agent_id} 是系统内置核心引擎，无需重复添加')

    preset = next((p for p in AGENT_PRESETS if p['id'] == agent_id), {})
    existing = load_custom_agents().get(agent_id, {})

    name = str(payload.get('name') or existing.get('name') or preset.get('name') or agent_id).strip()
    short_name = str(payload.get('short_name') or existing.get('short_name') or preset.get('short_name') or name[:10]).strip()
    adapter = str(payload.get('adapter') or existing.get('adapter') or preset.get('adapter') or 'cli').strip()
    if adapter not in ('cli', 'openai_http'):
        raise ValueError('adapter 仅支持 cli 或 openai_http')

    raw_models = payload.get('models') if 'models' in payload else (existing.get('models') or preset.get('models'))
    models = _normalize_models(raw_models, name)

    spec: Dict[str, Any] = {
        'id': agent_id,
        'name': name,
        'short_name': short_name,
        'description': str(payload.get('description') if 'description' in payload else (existing.get('description') or preset.get('description') or '')).strip(),
        'adapter': adapter,
        'enabled': bool(payload.get('enabled', existing.get('enabled', True))),
        'tone': str(payload.get('tone') or existing.get('tone') or preset.get('tone') or 'blue'),
        'models': models,
        'modes': payload.get('modes') or existing.get('modes') or _DEFAULT_MODES,
        'updated': time.time(),
    }

    if adapter == 'cli':
        binary = str(payload.get('binary') if 'binary' in payload else (existing.get('binary') or preset.get('binary') or agent_id)).strip()
        if not binary:
            raise ValueError('CLI Agent 必须指定可执行文件路径或命令名')
        args_template = payload.get('args_template') if 'args_template' in payload else (existing.get('args_template') or preset.get('args_template') or ['-p', '{prompt}'])
        if isinstance(args_template, str):
            args_template = [part for part in args_template.split(' ') if part]
        spec.update({
            'binary': binary,
            'args_template': list(args_template),
            'model_flag': str(payload.get('model_flag') if 'model_flag' in payload else (existing.get('model_flag', preset.get('model_flag', '--model')))).strip(),
            'resume_flag': str(payload.get('resume_flag') if 'resume_flag' in payload else (existing.get('resume_flag', preset.get('resume_flag', '')))).strip(),
            'mode_flags': payload.get('mode_flags') or existing.get('mode_flags') or preset.get('mode_flags') or {},
            'output_format': str(payload.get('output_format') or existing.get('output_format') or preset.get('output_format') or 'text'),
            'env': payload.get('env') if isinstance(payload.get('env'), dict) else (existing.get('env') or {}),
        })
    else:
        raw_base_url = str(payload.get('base_url') if 'base_url' in payload else (existing.get('base_url') or preset.get('base_url') or '')).strip()
        base_url = validate_agent_base_url(raw_base_url)
        api_key = str(payload.get('api_key') if 'api_key' in payload else (existing.get('api_key') or '')).strip()
        # Preserve existing masked key if user submits '***'
        if api_key == '***' and existing.get('api_key'):
            api_key = existing['api_key']
        spec.update({
            'base_url': base_url,
            'api_key': api_key,
            'api_key_env': str(payload.get('api_key_env') if 'api_key_env' in payload else (existing.get('api_key_env') or preset.get('api_key_env') or 'OPENAI_API_KEY')).strip(),
            'system_prompt': str(payload.get('system_prompt') if 'system_prompt' in payload else (existing.get('system_prompt') or '')).strip(),
        })

    cloud_url = str(payload.get('cloud_url', existing.get('cloud_url', ''))).strip()
    if cloud_url and not cloud_url.startswith('https://'):
        raise ValueError('云端入口必须使用 HTTPS 地址')
    spec['cloud_url'] = cloud_url
    custom = load_custom_agents()
    custom[agent_id] = spec
    save_custom_agents(custom)
    return sanitize_agent_for_client(spec)


def delete_custom_agent(agent_id: str) -> bool:
    if agent_id in BUILTIN_ENGINES:
        raise ValueError('不能删除系统内置引擎')
    custom = load_custom_agents()
    if agent_id not in custom:
        return False
    custom.pop(agent_id, None)
    save_custom_agents(custom)
    db.execute('DELETE FROM engine_models WHERE engine=?', (agent_id,))
    db.execute('DELETE FROM engine_checks WHERE engine=?', (agent_id,))
    return True


def sanitize_agent_for_client(spec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(spec)
    out['execution_targets'] = ['local', 'cloud'] if spec.get('cloud_url') else ['local']
    agent_id = out['id']
    if out.get('adapter') == 'cli':
        resolved = resolve_binary(out.get('binary', ''))
        out['resolved_binary'] = resolved
        out['installed'] = bool(resolved)
    elif out.get('adapter') == 'openai_http':
        has_key = bool(out.get('api_key') or os.environ.get(out.get('api_key_env') or ''))
        out['has_api_key'] = has_key
        if out.get('api_key'):
            out['api_key'] = '***'
        out['installed'] = bool(out.get('base_url'))
    row = db.one('SELECT payload FROM engine_checks WHERE engine=?', (agent_id,))
    check_state = json.loads(row['payload']) if row else {
        'status': 'ready' if out.get('installed') else 'unavailable',
        'code': None,
        'detail': '已就绪' if out.get('installed') else '未检测到对应 CLI 或配置',
        'checked_at': None,
    }
    out['check'] = check_state
    return out


def list_all_agents() -> List[Dict[str, Any]]:
    cfg = db.config()
    items: List[Dict[str, Any]] = []
    for builtin_id, label, short, tone in (
        ('codex', 'OpenAI Codex', 'Codex', 'green'),
        ('antigravity', 'Google Antigravity', 'AGY', 'purple'),
    ):
        bin_path = cfg.get(f'{builtin_id}_bin', '')
        installed = bool(bin_path and Path(bin_path).is_file())
        row = db.one('SELECT payload FROM engine_checks WHERE engine=?', (builtin_id,))
        check_state = json.loads(row['payload']) if row else {
            'status': 'ready' if installed else 'unavailable',
            'code': None,
            'detail': '已安装' if installed else '未安装',
            'checked_at': None,
        }
        cloud_url = cfg.get(f'{builtin_id}_cloud_url') or (
            'https://chatgpt.com/codex' if builtin_id == 'codex' else ''
        )
        has_cloud = (builtin_id == 'codex') or bool(cloud_url)
        items.append({
            'id': builtin_id,
            'name': label,
            'short_name': short,
            'builtin': True,
            'enabled': True,
            'adapter': 'builtin',
            'binary': bin_path,
            'cloud_url': cloud_url,
            'installed': installed,
            'tone': tone,
            'check': check_state,
            'execution_targets': ['local', 'cloud'] if has_cloud else ['local'],
        })
    for agent_id, spec in load_custom_agents().items():
        items.append({**sanitize_agent_for_client(spec), 'builtin': False})
    return items


async def check_custom_agent(agent_id: str) -> Dict[str, Any]:
    spec = get_agent_spec(agent_id)
    if not spec:
        raise ValueError(f'Agent {agent_id} 不存在')
    adapter = spec.get('adapter', 'cli')
    outcome: Dict[str, Any]
    if adapter == 'cli':
        resolved = resolve_binary(spec.get('binary', ''))
        if not resolved:
            outcome = {
                'status': 'unavailable',
                'code': 'not_installed',
                'detail': f"未找到可执行命令：{spec.get('binary')}（请确认已安装或在 PATH 中）",
            }
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    resolved, '--version',
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=8.0)
                ver = (out.decode(errors='replace').strip() or err.decode(errors='replace').strip()).splitlines()
                ver_line = ver[0][:120] if ver else 'CLI 响应正常'
                outcome = {
                    'status': 'ready',
                    'code': None,
                    'detail': f'CLI 就绪 ({resolved}) · {ver_line}',
                }
            except Exception as exc:
                outcome = {
                    'status': 'ready',
                    'code': None,
                    'detail': f'已定位二进制文件 ({resolved}) · {str(exc)[:80]}',
                }
    else:
        base_url = validate_agent_base_url(spec.get('base_url'))
        api_key = spec.get('api_key') or os.environ.get(spec.get('api_key_env') or '') or ''
        headers = {'Accept': 'application/json', 'User-Agent': 'Rove-Workstation/2.0'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        req = urllib.request.Request(f'{base_url}/models', headers=headers, method='GET')
        try:
            def _probe():
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return resp.status, resp.read(32768).decode('utf-8', errors='replace')
            status_code, body = await asyncio.to_thread(_probe)
            outcome = {
                'status': 'ready' if 200 <= status_code < 300 else 'unavailable',
                'code': None,
                'detail': f'API 连通正常 ({base_url} · HTTP {status_code})',
            }
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                outcome = {
                    'status': 'blocked',
                    'code': 'auth_error',
                    'detail': f'API 密钥未通过校验 (HTTP {exc.code})，请检查 API Key 或环境变量 {spec.get("api_key_env")}',
                }
            else:
                outcome = {
                    'status': 'unavailable',
                    'code': f'http_{exc.code}',
                    'detail': f'API 返回 HTTP {exc.code}: {exc.reason}',
                }
        except Exception as exc:
            outcome = {
                'status': 'unavailable',
                'code': 'connection_failed',
                'detail': f'无法连接 {base_url}: {str(exc)[:200]}',
            }

    outcome['checked_at'] = time.time()
    db.execute(
        'INSERT INTO engine_checks(engine,payload) VALUES(?,?) ON CONFLICT(engine) DO UPDATE SET payload=excluded.payload',
        (agent_id, json.dumps(outcome, ensure_ascii=False)),
    )
    return outcome


def get_session_native_agent_id(session_row: Optional[Dict[str, Any]], agent_id: str) -> Optional[str]:
    if not session_row:
        return None
    if agent_id == 'codex':
        return db.get_codex_native_id(session_row)
    if agent_id == 'antigravity':
        return db.get_antigravity_native_id(session_row)
    raw = session_row.get('native_agent_ids')
    if raw:
        try:
            mapping = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(mapping, dict) and mapping.get(agent_id):
                return str(mapping[agent_id])
        except Exception:
            pass
    if session_row.get('engine') == agent_id and session_row.get('native_id'):
        nid = session_row['native_id']
        if nid not in (session_row.get('native_codex_id'), session_row.get('native_antigravity_id')):
            return str(nid)
    return None


def set_session_native_agent_id(session_id: str, agent_id: str, native_id: str) -> None:
    sess = db.one('SELECT native_agent_ids FROM sessions WHERE id=?', (session_id,))
    mapping: Dict[str, str] = {}
    if sess and sess.get('native_agent_ids'):
        with contextlib.suppress(Exception):
            parsed = json.loads(sess['native_agent_ids'])
            if isinstance(parsed, dict):
                mapping = parsed
    mapping[agent_id] = native_id
    db.execute(
        'UPDATE sessions SET native_id=?, native_agent_ids=?, last_engine=? WHERE id=?',
        (native_id, json.dumps(mapping, ensure_ascii=False), agent_id, session_id),
    )


def _build_agent_prompt(
    agent_id: str,
    job: Dict[str, Any],
    session: Dict[str, Any],
    project: Dict[str, Any],
    images: List[Path],
) -> str:
    prompt_text = job['prompt']
    handoff_ctx = context_bridge.build_handoff_context(
        session, project, target_engine=agent_id, exclude_job_id=job['id']
    )
    native_id = get_session_native_agent_id(session, agent_id)
    if handoff_ctx:
        prompt_text = f'{handoff_ctx}\n\n{prompt_text}'
    elif not native_id:
        mem_ctx = memories.build_memory_context(project['id'])
        design_ctx = design_guard.get_design_directive(prompt_text=job['prompt'])
        parts = []
        if mem_ctx:
            parts.append(mem_ctx)
        if design_ctx:
            parts.append(design_ctx)
        parts.append(prompt_text)
        prompt_text = '\n\n'.join(parts)
    if images:
        refs = '\n'.join(f'- {p}' for p in images)
        prompt_text = f'{prompt_text}\n\n[附加本机图片文件]\n{refs}'
    return prompt_text


async def _run_cli_agent(
    spec: Dict[str, Any],
    job: Dict[str, Any],
    session: Dict[str, Any],
    project: Dict[str, Any],
    images: List[Path],
) -> Dict[str, Any]:
    agent_id = spec['id']
    resolved = resolve_binary(spec.get('binary', ''))
    if not resolved:
        return {
            'status': 'failed',
            'error': f"未找到 {spec.get('name', agent_id)} 的可执行文件 ({spec.get('binary')})，请在工作台设置中检查路径或安装该 CLI。",
        }

    prompt_text = _build_agent_prompt(agent_id, job, session, project, images)
    mode = job.get('mode') or session.get('mode') or 'standard'
    model = job.get('model') or session.get('model')
    native_id = get_session_native_agent_id(session, agent_id)

    args_template = spec.get('args_template') or ['-p', '{prompt}']
    cmd: List[str] = [resolved]
    prompt_injected = False
    for tok in args_template:
        if '{prompt}' in str(tok):
            cmd.append(str(tok).replace('{prompt}', prompt_text))
            prompt_injected = True
        else:
            cmd.append(str(tok))
    if not prompt_injected:
        cmd.append(prompt_text)

    model_flag = (spec.get('model_flag') or '').strip()
    if model and model_flag and model != 'default':
        cmd.extend([model_flag, model])

    resume_flag = (spec.get('resume_flag') or '').strip()
    if native_id and resume_flag:
        cmd.extend([resume_flag, native_id])

    mode_flags = (spec.get('mode_flags') or {}).get(mode) or []
    if isinstance(mode_flags, list):
        cmd.extend(str(x) for x in mode_flags if x)

    env = os.environ.copy()
    env['PATH'] = (
        f"{Path.home() / '.local/bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:"
        + env.get('PATH', '')
    )
    if isinstance(spec.get('env'), dict):
        for k, v in spec['env'].items():
            if k and v is not None:
                env[str(k)] = str(v)

    workdir = db.session_workdir(session, project)
    output_format = spec.get('output_format') or 'text'
    step_id = f'{agent_id}-reply-{job["id"]}'
    think_id = f'{agent_id}-think-{job["id"]}'

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )

    accumulated_text: List[str] = []
    accumulated_think: List[str] = []
    stderr_lines: List[str] = []

    async def read_stderr():
        async for raw_line in proc.stderr:
            line = raw_line.decode('utf-8', errors='replace').rstrip()
            if line:
                stderr_lines.append(line)

    async def read_stdout():
        async for raw_line in proc.stdout:
            line_str = raw_line.decode('utf-8', errors='replace')
            if output_format in ('claude-stream-json', 'stream-json', 'jsonl'):
                stripped = line_str.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    accumulated_text.append(line_str)
                    db.event(session['id'], job['id'], 'antigravity', {
                        'event': 'agy_step',
                        'step_id': step_id,
                        'step_kind': 'agent',
                        'engine': agent_id,
                        'text': ''.join(accumulated_text),
                        'completed': False,
                    })
                    continue

                # Extract native session/conversation id if present
                sid = payload.get('session_id') or payload.get('conversation_id')
                if sid and isinstance(sid, str):
                    set_session_native_agent_id(session['id'], agent_id, sid)

                # Handle Claude Code stream-json events
                ptype = payload.get('type')
                if ptype == 'assistant' and isinstance(payload.get('message'), dict):
                    for block in payload['message'].get('content') or []:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get('type')
                        if btype == 'thinking' and block.get('thinking'):
                            accumulated_think.append(block['thinking'])
                            db.event(session['id'], job['id'], 'antigravity', {
                                'event': 'agy_step',
                                'step_id': think_id,
                                'step_kind': 'reasoning',
                                'engine': agent_id,
                                'text': '\n\n'.join(accumulated_think),
                                'completed': False,
                            })
                        elif btype == 'text' and block.get('text'):
                            accumulated_text.append(block['text'])
                            db.event(session['id'], job['id'], 'antigravity', {
                                'event': 'agy_step',
                                'step_id': step_id,
                                'step_kind': 'agent',
                                'engine': agent_id,
                                'text': '\n\n'.join(accumulated_text),
                                'completed': False,
                            })
                        elif btype == 'tool_use':
                            tname = block.get('name') or 'tool'
                            tinput = block.get('input') or {}
                            tid = block.get('id') or f'{agent_id}-tool-{time.monotonic_ns()}'
                            if tname in ('Bash', 'bash', 'run_command') and tinput.get('command'):
                                db.event(session['id'], job['id'], 'antigravity', {
                                    'event': 'agy_step',
                                    'step_id': tid,
                                    'step_kind': 'command',
                                    'engine': agent_id,
                                    'command': tinput['command'],
                                    'cwd': workdir,
                                    'completed': True,
                                })
                            else:
                                db.event(session['id'], job['id'], 'antigravity', {
                                    'event': 'agy_step',
                                    'step_id': tid,
                                    'step_kind': 'tool',
                                    'engine': agent_id,
                                    'title': f'{spec.get("short_name", agent_id)} 调用：{tname}',
                                    'detail': json.dumps(tinput, ensure_ascii=False, indent=2)[:2000],
                                    'completed': True,
                                })
                elif ptype == 'result':
                    res_text = payload.get('result')
                    if isinstance(res_text, str) and res_text.strip() and not accumulated_text:
                        accumulated_text.append(res_text.strip())
                elif payload.get('event') in ('agy_step', 'step_update', 'result'):
                    payload['engine'] = agent_id
                    db.event(session['id'], job['id'], 'antigravity', payload)
            else:
                accumulated_text.append(line_str)
                db.event(session['id'], job['id'], 'antigravity', {
                    'event': 'agy_step',
                    'step_id': step_id,
                    'step_kind': 'agent',
                    'engine': agent_id,
                    'text': ''.join(accumulated_text),
                    'completed': False,
                })

    streams = [asyncio.create_task(read_stderr()), asyncio.create_task(read_stdout())]
    try:
        stop_time = None
        while proc.returncode is None:
            if db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))['stop']:
                if stop_time is None:
                    stop_time = time.monotonic()
                    proc.send_signal(signal.SIGINT)
                elif time.monotonic() - stop_time > 8:
                    proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 0.4)
            except asyncio.TimeoutError:
                pass
        await asyncio.gather(*streams)
        if stop_time:
            return {'status': 'stopped', 'error': None}

        final_text = ''.join(accumulated_text).strip()
        if accumulated_think:
            db.event(session['id'], job['id'], 'antigravity', {
                'event': 'agy_step',
                'step_id': think_id,
                'step_kind': 'reasoning',
                'engine': agent_id,
                'text': '\n\n'.join(accumulated_think),
                'completed': True,
            })
        if final_text:
            db.event(session['id'], job['id'], 'antigravity', {
                'event': 'agy_step',
                'step_id': step_id,
                'step_kind': 'agent',
                'engine': agent_id,
                'text': final_text,
                'completed': True,
            })

        if proc.returncode == 0:
            return {'status': 'completed', 'error': None}
        err_msg = '\n'.join(stderr_lines)[-2000:] or f'{spec.get("name", agent_id)} 退出码 {proc.returncode}'
        return {'status': 'failed', 'error': err_msg}
    finally:
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 4)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.gather(*streams, return_exceptions=True)


async def _run_openai_http_agent(
    spec: Dict[str, Any],
    job: Dict[str, Any],
    session: Dict[str, Any],
    project: Dict[str, Any],
    images: List[Path],
) -> Dict[str, Any]:
    agent_id = spec['id']
    try:
        base_url = validate_agent_base_url(spec.get('base_url'))
    except ValueError as exc:
        return {'status': 'failed', 'error': f'{spec.get("name", agent_id)} API 地址无效：{exc}'}

    api_key = (spec.get('api_key') or '').strip() or os.environ.get(spec.get('api_key_env') or '', '').strip()
    model = job.get('model') or session.get('model') or (spec.get('models') or [{}])[0].get('id') or 'deepseek-chat'
    prompt_text = _build_agent_prompt(agent_id, job, session, project, images)
    workdir = db.session_workdir(session, project)

    sys_prompt = (
        spec.get('system_prompt')
        or f"You are {spec.get('name', agent_id)}, an expert AI software engineering agent inside Rove Workstation. "
           f"Current project: {project.get('name')} ({workdir}). Provide clear, accurate, production-ready code and analysis."
    )

    # Build multi-turn messages from recent session turns if continuing same engine
    messages: List[Dict[str, str]] = [{'role': 'system', 'content': sys_prompt}]
    prev_turns = context_bridge.extract_session_turns(session['id'], exclude_job_id=job['id'])
    for t in prev_turns[-6:]:
        if t.get('prompt'):
            messages.append({'role': 'user', 'content': t['prompt']})
        if t.get('reply'):
            messages.append({'role': 'assistant', 'content': t['reply']})
    messages.append({'role': 'user', 'content': prompt_text})

    step_id = f'{agent_id}-reply-{job["id"]}'
    think_id = f'{agent_id}-think-{job["id"]}'

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream, application/json',
        'User-Agent': 'Rove-Workstation/2.0',
    }
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    body_bytes = json.dumps({
        'model': model,
        'messages': messages,
        'stream': True,
    }, ensure_ascii=False).encode('utf-8')

    req = urllib.request.Request(f'{base_url}/chat/completions', data=body_bytes, headers=headers, method='POST')

    def _stream_request():
        text_parts: List[str] = []
        think_parts: List[str] = []
        last_emit = time.monotonic()

        with urllib.request.urlopen(req, timeout=180) as resp:
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'text/event-stream' not in ctype:
                raw = json.loads(resp.read().decode('utf-8', errors='replace'))
                choice = (raw.get('choices') or [{}])[0].get('message') or {}
                r_think = choice.get('reasoning_content') or ''
                r_text = choice.get('content') or ''
                if r_think:
                    think_parts.append(r_think)
                if r_text:
                    text_parts.append(r_text)
            else:
                last_stop_check = time.monotonic()
                for raw_line in resp:
                    now_loop = time.monotonic()
                    if now_loop - last_stop_check >= 0.5:
                        last_stop_check = now_loop
                        if db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))['stop']:
                            return 'stopped', ''.join(text_parts), ''.join(think_parts)
                    line = raw_line.decode('utf-8', errors='replace').strip()
                    if not line or not line.startswith('data:'):
                        continue
                    data_str = line[5:].strip()
                    if data_str == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = ((chunk.get('choices') or [{}])[0].get('delta')) or {}
                    d_think = delta.get('reasoning_content') or ''
                    d_text = delta.get('content') or ''
                    if d_think:
                        think_parts.append(d_think)
                    if d_text:
                        text_parts.append(d_text)
                    now = time.monotonic()
                    if now - last_emit >= 0.15:
                        if think_parts:
                            db.event(session['id'], job['id'], 'antigravity', {
                                'event': 'agy_step',
                                'step_id': think_id,
                                'step_kind': 'reasoning',
                                'engine': agent_id,
                                'text': ''.join(think_parts),
                                'completed': False,
                            })
                        if text_parts:
                            db.event(session['id'], job['id'], 'antigravity', {
                                'event': 'agy_step',
                                'step_id': step_id,
                                'step_kind': 'agent',
                                'engine': agent_id,
                                'text': ''.join(text_parts),
                                'completed': False,
                            })
                        last_emit = now
        return 'completed', ''.join(text_parts), ''.join(think_parts)

    try:
        status, final_text, final_think = await asyncio.to_thread(_stream_request)
        if final_think:
            db.event(session['id'], job['id'], 'antigravity', {
                'event': 'agy_step',
                'step_id': think_id,
                'step_kind': 'reasoning',
                'engine': agent_id,
                'text': final_think,
                'completed': True,
            })
        if final_text:
            db.event(session['id'], job['id'], 'antigravity', {
                'event': 'agy_step',
                'step_id': step_id,
                'step_kind': 'agent',
                'engine': agent_id,
                'text': final_text,
                'completed': True,
            })
        set_session_native_agent_id(session['id'], agent_id, f'{agent_id}_{session["id"]}')
        if status == 'stopped':
            return {'status': 'stopped', 'error': None}
        return {'status': 'completed', 'error': None}
    except urllib.error.HTTPError as exc:
        err_body = ''
        with contextlib.suppress(Exception):
            err_body = exc.read(2000).decode('utf-8', errors='replace')
        return {'status': 'failed', 'error': f'API 请求失败 (HTTP {exc.code}): {err_body or exc.reason}'}
    except Exception as exc:
        return {'status': 'failed', 'error': f'调用 {spec.get("name", agent_id)} 失败：{str(exc)[:1500]}'}


async def run_custom_agent(
    agent_id: str,
    job: Dict[str, Any],
    session: Dict[str, Any],
    project: Dict[str, Any],
    images: List[Path],
) -> Dict[str, Any]:
    spec = get_agent_spec(agent_id)
    if not spec or not spec.get('enabled', True):
        return {'status': 'failed', 'error': f'未找到或未启用 Agent：{agent_id}'}
    if spec.get('adapter') == 'openai_http':
        return await _run_openai_http_agent(spec, job, session, project, images)
    return await _run_cli_agent(spec, job, session, project, images)
