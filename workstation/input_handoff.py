"""Zero-trace secret input handoff for remote/mobile coding workflows.

Inspired by makecindy/cindy's Input Handoff invariants:
- Secret values entered by the user NEVER enter SQLite events/approvals,
  model context, logs, or push notifications.
- Ephemeral in-memory queues deliver secret values directly to the waiting
  worker/PTY and are wiped immediately on read.
- Anti-phishing origin checks flag http://, IP literals, punycode, or localhost.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import store as db

HANDOFF_TTL_SECS = 900.0


def _handoff_dir() -> Path:
    d = db.DATA / 'handoffs'
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _handoff_file(handoff_id: str) -> Path:
    safe = ''.join(ch for ch in str(handoff_id) if ch.isalnum())
    return _handoff_dir() / f'{safe}.json'


def _sweep_expired_handoffs() -> None:
    now = time.time()
    d = db.DATA / 'handoffs'
    if not d.is_dir():
        return
    for p in d.glob('*.json'):
        try:
            if (now - p.stat().st_mtime) > HANDOFF_TTL_SECS:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def classify_origin_warning(origin: str) -> Optional[str]:
    """Return a user-facing phishing/transport warning if origin is untrusted."""
    raw = (origin or '').strip()
    if not raw:
        return None
    if raw.startswith(('http://', 'file:', 'data:')):
        return f'非加密或本地来源 ({raw.split(":", 1)[0]})，请确认目标环境可信'
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ''
    except Exception:
        return None
    if not host:
        return None
    if host.startswith('xn--') or '.xn--' in host:
        return f'检测到 Punycode 国际化域名 ({host})，请警惕仿冒'
    if host in ('localhost', '127.0.0.1', '::1'):
        return '目标为本机回环地址，请确认确为宿主机服务'
    try:
        ipaddress.ip_address(host)
        return f'目标为裸 IP 地址 ({host})，请核实来源身份'
    except ValueError:
        pass
    return None


def create_secret_handoff(
    job_id: str,
    session_id: str,
    purpose: str,
    fields: List[Dict[str, Any]],
    origin_label: str = '',
) -> Dict[str, Any]:
    """Create a metadata-only secret handoff record in SQLite."""
    ident = secrets.token_hex(10)
    now = time.time()
    safe_fields = [
        {
            'id': str(f.get('id') or f'field_{i}'),
            'label': str(f.get('label') or f.get('question') or '敏感输入'),
            'isSecret': bool(f.get('isSecret', True)),
        }
        for i, f in enumerate(fields)
    ]
    db.execute(
        'INSERT INTO secret_handoffs(id, job_id, session_id, purpose, origin_label, fields_json, '
        'status, steps, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (
            ident,
            job_id,
            session_id,
            purpose,
            origin_label,
            json.dumps(safe_fields, ensure_ascii=False),
            'pending',
            0,
            now,
            now,
        ),
    )
    return db.one('SELECT * FROM secret_handoffs WHERE id=?', (ident,))


def submit_secret_values(handoff_id: str, values: Dict[str, str]) -> bool:
    """Store secret values in a 0600 ephemeral IPC file (cross-process safe, never in SQLite)."""
    _sweep_expired_handoffs()
    clean_values = {str(k): str(v) for k, v in (values or {}).items() if v is not None}
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, session_id, job_id, status FROM secret_handoffs WHERE id=?",
            (handoff_id,),
        ).fetchone()
        if not row or row['status'] != 'pending':
            return False
        fpath = _handoff_file(handoff_id)
        fpath.unlink(missing_ok=True)
        fd = os.open(fpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(clean_values, f, ensure_ascii=False)
        conn.execute(
            "UPDATE secret_handoffs SET status='submitted', steps=steps+1, updated=? WHERE id=?",
            (time.time(), handoff_id),
        )
    return True


def pop_secret_values(handoff_id: str) -> Optional[Dict[str, str]]:
    """Atomically read and unlink the ephemeral 0600 secret IPC file."""
    fpath = _handoff_file(handoff_id)
    if not fpath.is_file():
        return None
    try:
        box = json.loads(fpath.read_text(encoding='utf-8'))
    except Exception:
        box = None
    finally:
        fpath.unlink(missing_ok=True)
    if box is not None:
        db.execute(
            "UPDATE secret_handoffs SET status='consumed', updated=? WHERE id=? AND status='submitted'",
            (time.time(), handoff_id),
        )
    return box


def cancel_secret_handoff(handoff_id: str) -> None:
    _handoff_file(handoff_id).unlink(missing_ok=True)
    db.execute(
        "UPDATE secret_handoffs SET status='cancelled', updated=? WHERE id=? AND status='pending'",
        (time.time(), handoff_id),
    )
