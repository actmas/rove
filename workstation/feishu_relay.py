"""Feishu (Lark) Channel Adapter & Approval Relay for Rove Workstation.

Inspired by deepseek-ai/dsh-superagent-prototype. Integrates Feishu as an
out-of-band notification, interactive approval, and status monitoring channel.
"""
import asyncio
import hmac
import json
from pathlib import Path
import secrets
import time
import urllib.request
from typing import Any, Dict, List, Optional

from . import store as db

FEISHU_BRIDGE_CONFIG = Path.home() / '.config/feishu-codex/config.json'


def load_feishu_config() -> Optional[Dict[str, Any]]:
    """Load Feishu configuration from workstation data or existing feishu-codex bridge."""
    target = None
    ws_cfg = db.DATA / 'feishu.json'
    if ws_cfg.is_file():
        target = ws_cfg
    elif FEISHU_BRIDGE_CONFIG.is_file():
        target = FEISHU_BRIDGE_CONFIG

    if not target:
        return None

    try:
        data = json.loads(target.read_text(encoding='utf-8'))
        if data.get('app_id') and data.get('app_secret'):
            return data
    except Exception:
        pass
    return None


class FeishuAPI:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = None
        self.expires = 0

    def request(self, path: str, data: Dict[str, Any], authenticated: bool = True) -> Dict[str, Any]:
        if authenticated and (not self.token or time.time() >= self.expires):
            res = self.request(
                '/auth/v3/tenant_access_token/internal',
                {'app_id': self.app_id, 'app_secret': self.app_secret},
                authenticated=False,
            )
            self.token = res.get('tenant_access_token')
            self.expires = time.time() + res.get('expire', 7200) - 120

        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if authenticated and self.token:
            headers['Authorization'] = f'Bearer {self.token}'

        req = urllib.request.Request(
            f'https://open.feishu.cn/open-apis{path}',
            data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))

        if body.get('code', 0) != 0:
            raise RuntimeError(f"Feishu API error {body.get('code')}: {body.get('msg')}")
        return body

    async def send_text(self, receive_id: str, text: str, receive_id_type: str = 'open_id') -> Dict[str, Any]:
        """Send a plain or formatted markdown text message."""
        payload = {
            'receive_id': receive_id,
            'msg_type': 'text',
            'content': json.dumps({'text': text[:3800]}, ensure_ascii=False),
            'uuid': secrets.token_hex(16),
        }
        return await asyncio.to_thread(
            self.request,
            f'/im/v1/messages?receive_id_type={receive_id_type}',
            payload,
        )


def get_target_receive_id(cfg: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Determine the target open_id or chat_id configured to receive notifications."""
    # 1. Check allowed_open_ids list
    allowed = cfg.get('allowed_open_ids')
    if isinstance(allowed, list) and allowed:
        return allowed[0], 'open_id'
    # 2. Check single target
    target = cfg.get('target_chat_id') or cfg.get('chat_id')
    if target:
        return target, 'chat_id'
    open_id = cfg.get('paired_open_id') or cfg.get('open_id')
    if open_id:
        return open_id, 'open_id'
    return None


async def send_approval_notification(
    session: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    approval_id: str,
    method: str,
    params: Dict[str, Any],
) -> bool:
    """Send an interactive approval alert to Feishu."""
    cfg = load_feishu_config()
    if not cfg:
        return False
    target = get_target_receive_id(cfg)
    if not target:
        return False

    receive_id, id_type = target
    api = FeishuAPI(cfg['app_id'], cfg['app_secret'])

    proj_name = (project or {}).get('name') or 'Rove'
    sess_title = (session or {}).get('title') or '当前会话'

    lines = [
        f"🚨【Rove 工作台 · 任务待审批】",
        f"项目：{proj_name}",
        f"会话：{sess_title}",
    ]

    if method == 'item/tool/requestUserInput':
        questions = params.get('questions') or []
        first_q = (questions[0].get('question') if questions and isinstance(questions[0], dict) else '') or '请回答交互问题'
        lines.append(f"提问：{first_q}")
    else:
        cmd = params.get('command') or params.get('reason') or params.get('method') or '请求执行操作'
        if isinstance(cmd, list):
            cmd = ' '.join(str(x) for x in cmd)
        lines.append(f"待执行命令/操作：\n`{cmd}`")

    lines.append("")
    lines.append("⚡ 快捷审批指令（直接回复此机器人）：")
    lines.append(f"• 批准执行：/approve {approval_id}")
    lines.append(f"• 拒绝中断：/reject {approval_id}")
    lines.append(f"• 查看状态：/tasks")

    text = '\n'.join(lines)
    try:
        await api.send_text(receive_id, text, receive_id_type=id_type)
        return True
    except Exception:
        return False


async def send_outcome_notification(
    job_id: str,
    session: Dict[str, Any],
    project: Optional[Dict[str, Any]],
    outcome: Dict[str, Any],
) -> bool:
    """Send a task completion / failure notice to Feishu."""
    status = (outcome or {}).get('status')
    if status not in ('completed', 'failed', 'stopped', 'submitted'):
        return False

    cfg = load_feishu_config()
    if not cfg:
        return False
    target = get_target_receive_id(cfg)
    if not target:
        return False

    receive_id, id_type = target
    api = FeishuAPI(cfg['app_id'], cfg['app_secret'])

    proj_name = (project or {}).get('name') or 'Rove'
    sess_title = (session or {}).get('title') or '当前会话'

    if status == 'completed':
        header = f"✅【Rove 工作台 · 任务完成】"
        detail = "AI 编码任务已成功执行完成，修改已保存至工作区。"
    elif status == 'submitted':
        header = f"☁️【Rove 工作台 · 云端任务已提交】"
        detail = "任务已成功提交至 Codex 云端独立运行，可在网页端查看执行状态。"
    elif status == 'stopped':
        header = f"⏹️【Rove 工作台 · 任务已停止】"
        detail = "任务已被手动中断，已发生的修改已安全保留。"
    else:
        header = f"❌【Rove 工作台 · 任务异常】"
        err = (outcome.get('error') or '执行中断').strip()
        detail = f"错误信息：{err[:200]}"

    text = f"{header}\n项目：{proj_name}\n会话：{sess_title}\n{detail}\n\n可在 Rove 网页端或回复 /status 查看完整进展。"
    try:
        await api.send_text(receive_id, text, receive_id_type=id_type)
        return True
    except Exception:
        return False


def get_feishu_status() -> Dict[str, Any]:
    cfg = load_feishu_config()
    if not cfg:
        return {
            'configured': False,
            'app_id': None,
            'has_target': False,
        }
    target = get_target_receive_id(cfg)
    user_authorized = bool(cfg.get('allowed_open_ids') or cfg.get('paired_open_id') or cfg.get('open_id'))
    authorization_mode = 'users' if user_authorized else 'chat' if target else 'unconfigured'
    return {
        'configured': True,
        'app_id': cfg.get('app_id', '')[:8] + '...',
        'has_target': bool(target),
        'target_type': target[1] if target else None,
        'authorization_mode': authorization_mode,
        'inbound_ready': bool(cfg.get('verification_token') and target),
        'inbound_detail': ('仅授权用户可发送指令' if user_authorized else '配置群聊内的成员可发送指令') if cfg.get('verification_token') and target else '请配置 verification_token 和接收人或群聊',
    }


def _apply_approval_decision(approval_id: str | None, decision: str, event_key: str | None = None) -> tuple[bool, str]:
    if not approval_id:
        return False, '⚠️ 请提供审批单号，例如 /approve <approval_id>。'
    row = db.one(
        "SELECT a.*, j.session_id, j.status FROM approvals a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (approval_id.strip(),),
    )
    if not row or row.get('status') != 'waiting' or row.get('decision') is not None:
        return False, '⚠️ 未找到等待中的审批项，或该审批已处理。'
    if row.get('method') == 'item/tool/requestUserInput':
        return False, '⚠️ 该审批为问答交互题，请在 Rove 网页端完成作答。'
    if not db.decide_approval(row['id'], decision, event_key):
        return False, '⚠️ 该审批已处理、消息已处理或任务已结束。'
    label = '✅ 已批准执行' if decision == 'accept' else '⏹️ 已拒绝该操作'
    return True, f"{label}（审批单号：{row['id']}）。"


def _build_tasks_summary() -> str:
    active_jobs = db.query(
        "SELECT j.id, j.status, j.engine, j.started, s.title FROM jobs j "
        "JOIN sessions s ON s.id=j.session_id "
        "WHERE j.status IN ('queued', 'running', 'waiting') ORDER BY j.started DESC LIMIT 5"
    )
    if not active_jobs:
        latest = db.one("SELECT title, engine, updated FROM sessions ORDER BY updated DESC LIMIT 1")
        if latest:
            return f"🟢 Rove 工作台当前空闲就绪\n最近会话：{latest.get('title')} ({latest.get('engine')})"
        return "🟢 Rove 工作台当前空闲就绪，暂无运行中的任务。"
    lines = ["📊【Rove 工作台 · 当前活跃任务】"]
    now = time.time()
    for j in active_jobs:
        elapsed = int(max(0, now - (j.get('started') or now)))
        lines.append(f"• [{j['status'].upper()}] {j.get('title') or '未命名'} ({j.get('engine') or 'codex'}, 已运行 {elapsed}s)")
    pending = db.one(
        "SELECT a.id, a.method FROM approvals a JOIN jobs j ON j.id=a.job_id "
        "WHERE j.status='waiting' AND a.decision IS NULL ORDER BY a.created DESC LIMIT 1"
    )
    if pending:
        lines.append(f"\n⚠️ 待审批单号：{pending['id']}\n回复 `/approve {pending['id']}` 批准，或 `/reject {pending['id']}` 拒绝。")
    return '\n'.join(lines)


async def handle_webhook_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle Feishu URL verification challenge, IM message commands (/approve, /reject, /tasks, /status), and card actions."""
    cfg = load_feishu_config() or {}
    header = payload.get('header') or {}
    expected_token = cfg.get('verification_token')
    incoming_token = header.get('token') or payload.get('token')
    if not expected_token or not isinstance(incoming_token, str) or not hmac.compare_digest(incoming_token, expected_token):
        raise PermissionError('飞书事件 Token 校验失败，请配置 verification_token')
    if payload.get('type') == 'url_verification':
        return {'challenge': payload.get('challenge', '')}

    event = payload.get('event') or {}
    message = event.get('message') or {}
    sender_open_id = ((event.get('sender') or {}).get('sender_id') or {}).get('open_id') or ''
    action = event.get('action') or payload.get('action')
    operator = event.get('operator') or payload.get('operator') or {}
    operator_id = (operator.get('operator_id') or {}).get('open_id') or operator.get('open_id') or payload.get('open_id')
    context = event.get('context') or payload.get('context') or {}
    chat_id = message.get('chat_id') or context.get('open_chat_id') or payload.get('open_chat_id')
    allowed_ids = cfg.get('allowed_open_ids') or []
    paired_id = cfg.get('paired_open_id') or cfg.get('open_id')
    if not allowed_ids and paired_id:
        allowed_ids = [paired_id]
    actor_id = operator_id if isinstance(action, dict) else sender_open_id
    target_chat = cfg.get('target_chat_id') or cfg.get('chat_id')
    authorized = actor_id in allowed_ids if allowed_ids else bool(actor_id and target_chat and chat_id == target_chat)
    if not authorized:
        return {'ok': False, 'ignored': 'sender_not_authorized'}

    event_id = header.get('event_id') or message.get('message_id') or event.get('token')
    event_key = 'feishu:' + str(event_id) if event_id else None
    if isinstance(action, dict):
        val = action.get('value') or {}
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                val = {}
        if not isinstance(val, dict):
            return {'ok': False, 'ignored': 'invalid_action'}
        decision = val.get('decision')
        if decision in ('accept', 'decline'):
            ok, text = _apply_approval_decision(val.get('approval_id'), decision, event_key)
            return {'ok': ok, 'toast': {'type': 'success' if ok else 'warning', 'content': text}}

    raw_content = message.get('content') or ''
    text = ''
    if isinstance(raw_content, str) and raw_content.strip():
        try:
            text = (json.loads(raw_content).get('text') or '').strip()
        except Exception:
            text = raw_content.strip()
    elif isinstance(raw_content, dict):
        text = (raw_content.get('text') or '').strip()

    if not text:
        return {'ok': True, 'ignored': 'empty_text'}

    # Strip @bot mentions if any
    cleaned = ' '.join(part for part in text.split() if not part.startswith('@_user_')).strip()
    parts = cleaned.split()
    if not parts:
        return {'ok': True, 'ignored': 'no_command'}

    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None
    reply_text = None

    if cmd in ('/approve', 'approve', '批准', '同意'):
        _, reply_text = _apply_approval_decision(arg, 'accept', event_key)
    elif cmd in ('/reject', '/decline', 'reject', 'decline', '拒绝'):
        _, reply_text = _apply_approval_decision(arg, 'decline', event_key)
    elif cmd in ('/tasks', '/status', 'tasks', 'status', '状态'):
        reply_text = _build_tasks_summary()

    if reply_text and cfg.get('app_id') and cfg.get('app_secret'):
        api = FeishuAPI(cfg['app_id'], cfg['app_secret'])
        target_id = sender_open_id or chat_id
        target_type = 'open_id' if sender_open_id else 'chat_id'
        if target_id:
            try:
                await api.send_text(target_id, reply_text, receive_id_type=target_type)
            except Exception:
                pass

    return {'ok': True, 'handled': bool(reply_text), 'reply': reply_text}
