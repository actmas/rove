"""Multi-subscription account pool for single-machine multi-account isolation.

Inspired by makecindy/cindy's subscription-account-pools:
- Each Codex account uses an isolated CODEX_HOME directory (default: ~/.codex).
- Each Claude Code account uses an isolated CLAUDE_CONFIG_DIR directory (~/.claude).
- Rove never reads or copies OAuth tokens; official CLIs manage login in their own dir.
- Supports pausing an account, rotating to the next healthy account, and passing
  isolated env vars when spawning CodexRPC or Claude CLI subprocesses.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from . import store as db

SUPPORTED_POOL_ENGINES = frozenset({'codex', 'claude-code', 'antigravity'})
SLOT_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,31}$')

_DEFAULT_LABELS = {
    'codex': 'Codex',
    'claude-code': 'Claude',
    'antigravity': 'Google',
}


def default_account_dir(engine: str, slot_id: str) -> Path:
    safe_slot = re.sub(r'[^a-z0-9_-]', '-', slot_id.lower()).strip('-') or secrets.token_hex(4)
    return (db.DATA / 'accounts' / f'{engine}-{safe_slot}').resolve()


def list_accounts(engine: Optional[str] = None, include_paused: bool = True) -> List[Dict[str, Any]]:
    sql = 'SELECT * FROM subscription_accounts'
    clauses = []
    args: List[Any] = []
    if engine:
        clauses.append('engine=?')
        args.append(engine)
    if not include_paused:
        clauses.append('paused=0')
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY priority ASC, created ASC'
    return db.query(sql, tuple(args))


def add_account(
    engine: str,
    label: str,
    home_dir: Optional[str] = None,
    priority: int = 0,
) -> Dict[str, Any]:
    if engine not in SUPPORTED_POOL_ENGINES:
        raise ValueError(f'暂不支持该引擎的多账号池: {engine}（目前支持 codex / claude-code）')
    clean_label = (label or '').strip()
    if not clean_label:
        raise ValueError('账号名称不能为空')

    ident = secrets.token_hex(8)
    target_dir = Path(home_dir).expanduser().resolve() if home_dir else default_account_dir(engine, ident[:8])
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    db.execute(
        'INSERT INTO subscription_accounts(id, engine, label, home_dir, paused, priority, created, updated) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (ident, engine, clean_label, str(target_dir), 0, int(priority), now, now),
    )
    return db.one('SELECT * FROM subscription_accounts WHERE id=?', (ident,))


def set_account_paused(account_id: str, paused: bool) -> Optional[Dict[str, Any]]:
    row = db.one('SELECT * FROM subscription_accounts WHERE id=?', (account_id,))
    if not row:
        return None
    db.execute(
        'UPDATE subscription_accounts SET paused=?, updated=? WHERE id=?',
        (1 if paused else 0, time.time(), account_id),
    )
    return db.one('SELECT * FROM subscription_accounts WHERE id=?', (account_id,))


def remove_account(account_id: str, purge_dir: bool = False) -> bool:
    row = db.one('SELECT id, home_dir FROM subscription_accounts WHERE id=?', (account_id,))
    if not row:
        return False
    db.execute('UPDATE sessions SET account_id=NULL WHERE account_id=?', (account_id,))
    db.execute('DELETE FROM subscription_accounts WHERE id=?', (account_id,))
    if purge_dir and row.get('home_dir'):
        accounts_root = (db.DATA / 'accounts').resolve()
        target = Path(row['home_dir']).resolve()
        if target != accounts_root and target.is_relative_to(accounts_root) and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    return True


def is_account_authenticated(row: Dict[str, Any]) -> bool:
    """Check if the isolated account directory already holds CLI login state."""
    home_dir = row.get('home_dir') or ''
    if not home_dir:
        return False
    p = Path(home_dir)
    if not p.is_dir():
        return False
    engine = row.get('engine')
    if engine == 'codex':
        return (p / 'auth.json').is_file()
    if engine == 'claude-code':
        return (p / '.credentials.json').is_file() or (p / '.claude.json').is_file()
    if engine == 'antigravity':
        return any(p.rglob('*auth*')) or any(p.rglob('*token*')) or any(p.rglob('*.json'))
    return False


def quick_login_account(engine: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Zero-config direct login: auto-create slot, auto-label, and launch CLI OAuth/device-auth."""
    if engine not in SUPPORTED_POOL_ENGINES:
        raise ValueError(f'暂不支持该引擎: {engine}')
    existing = list_accounts(engine=engine)
    auto_label = (label or '').strip() or f"{_DEFAULT_LABELS.get(engine, engine)} #{len(existing) + 1}"
    row = add_account(engine, auto_label)
    login_res = start_account_login(row['id'])
    row['authenticated'] = is_account_authenticated(row)
    row['login_command'] = login_res.get('login_command', '')
    row['login_url'] = login_res.get('login_url', '')
    row['device_code'] = login_res.get('device_code', '')
    row['login_note'] = login_res.get('note', '')
    row['login_started'] = bool(login_res.get('ok'))
    return row


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_URL_RE = re.compile(r'https://[^\s<>"\'\)]+')
_DEVICE_CODE_RE = re.compile(r'(?m)^\s*([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\s*$')


def start_account_login(account_id: str) -> Dict[str, Any]:
    """Launch CLI login (preferring device-auth for remote/mobile browsers) and capture URL + code."""
    row = db.one('SELECT * FROM subscription_accounts WHERE id=?', (account_id,))
    if not row:
        raise ValueError('账号不存在')
    engine = row.get('engine')
    env = os.environ.copy()
    env.update(account_env_for_row(row))
    if engine == 'codex':
        binary = db.config().get('codex_bin') or shutil.which('codex') or 'codex'
        cmd = [binary, 'login', '--device-auth']
    elif engine == 'claude-code':
        binary = shutil.which('claude') or 'claude'
        cmd = [binary, 'auth', 'login']
    elif engine == 'antigravity':
        binary = db.config().get('antigravity_bin') or shutil.which('agy') or 'agy'
        cmd = [binary, '-p', 'hi']
    else:
        raise ValueError(f'暂不支持自动启动登录: {engine}')

    log_path = Path(row['home_dir']) / '.login-output.log'
    log_file = open(log_path, 'wb')
    try:
        subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_file.close()

    login_url = ''
    device_code = ''
    deadline = time.monotonic() + 3.5
    while time.monotonic() < deadline:
        try:
            raw_text = log_path.read_text(encoding='utf-8', errors='replace')
            text = _ANSI_RE.sub('', raw_text)
        except OSError:
            text = ''
        m_url = _URL_RE.search(text)
        m_code = _DEVICE_CODE_RE.search(text)
        if m_url:
            login_url = m_url.group(0).rstrip('.,;)')
        if m_code:
            device_code = m_code.group(1)
        if login_url and (device_code or engine != 'codex'):
            break
        time.sleep(0.2)

    note = ''
    if not login_url:
        if engine == 'antigravity':
            note = 'Google / Antigravity CLI 暂不支持无头设备码，请在 Mac 本机 Antigravity / Gemini 客户端登录或运行 agy 完成首次授权。'
        else:
            note = '未捕获到授权链接，请确认本机已安装对应 CLI 或在终端运行登录命令。'

    return {
        'ok': True,
        'id': account_id,
        'engine': engine,
        'login_url': login_url,
        'device_code': device_code,
        'note': note,
        'login_command': login_command_hint(row),
    }


def account_env_for_row(row: Dict[str, Any]) -> Dict[str, str]:
    engine = row.get('engine')
    home_dir = row.get('home_dir') or ''
    if not home_dir:
        return {}
    if engine == 'codex':
        return {'CODEX_HOME': home_dir}
    if engine == 'claude-code':
        return {'CLAUDE_CONFIG_DIR': home_dir}
    if engine == 'antigravity':
        return {'XDG_CONFIG_HOME': home_dir}
    return {}


def resolve_account_env_for_session(session: Dict[str, Any], engine: str) -> Dict[str, str]:
    """Resolve isolated env vars for a session's bound or next authenticated pool account."""
    if engine not in SUPPORTED_POOL_ENGINES:
        return {}
    acct_id = session.get('account_id')
    if acct_id:
        row = db.one('SELECT * FROM subscription_accounts WHERE id=? AND engine=?', (acct_id, engine))
        if row and not row.get('paused') and is_account_authenticated(row):
            return account_env_for_row(row)
    # Only pick an authenticated, non-paused pool account; otherwise fall back to host default CLI login
    for row in list_accounts(engine=engine, include_paused=False):
        if is_account_authenticated(row):
            return account_env_for_row(row)
    return {}


def switch_to_next_account(session_id: str, engine: str, current_account_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Rotate a session to the next authenticated, non-paused account in the pool."""
    avail = [a for a in list_accounts(engine=engine, include_paused=False) if is_account_authenticated(a)]
    if not avail:
        return None
    candidates = [a for a in avail if a['id'] != current_account_id]
    if not candidates:
        return None
    nxt = candidates[0]
    db.execute('UPDATE sessions SET account_id=?, updated=? WHERE id=?', (nxt['id'], time.time(), session_id))
    return nxt


def login_command_hint(account_row: Dict[str, Any]) -> str:
    """Return the shell command the user can run once to authenticate this isolated account slot."""
    engine = account_row.get('engine')
    home = account_row.get('home_dir')
    if engine == 'codex':
        return f'CODEX_HOME="{home}" codex login'
    if engine == 'claude-code':
        return f'CLAUDE_CONFIG_DIR="{home}" claude'
    return ''
