"""Usage-limit auto-continue scheduler for subscription AI engines.

Inspired by makecindy/cindy's usage-limit auto-continue: when a subscription
engine (Codex, Antigravity, Claude Code) hits a 5-hour or weekly quota cap,
persist the recovery intent with the resolved reset timestamp (+ buffer) and
resume automatically once quota resets, unless the user cancels, switches
models, or sends a new message.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple

from . import store as db

SUBSCRIPTION_ENGINES = frozenset({'codex', 'antigravity', 'claude-code'})
MAX_AUTO_CONTINUE_ATTEMPTS = 3
DEFAULT_BUFFER_SECS = 60.0

_QUOTA_ERROR_PATTERNS = (
    re.compile(r'usage\s*limit', re.IGNORECASE),
    re.compile(r'quota\s*(?:exceeded|exhausted|limit|reached)', re.IGNORECASE),
    re.compile(r'5[- ]?hour\s*(?:limit|window|cap)', re.IGNORECASE),
    re.compile(r'weekly\s*(?:limit|quota|cap)', re.IGNORECASE),
    re.compile(r'额度(?:已用尽|不足|上限|耗尽)|配额(?:已用尽|上限)'),
    re.compile(r'try\s+again\s+(?:at|in)\s+~?\d+', re.IGNORECASE),
    re.compile(r'resets?\s+(?:at|in)\s+', re.IGNORECASE),
)

_RETRY_IN_MIN_RE = re.compile(r'try\s+again\s+in\s+~?(\d+)\s*(?:min|minute|m)\b', re.IGNORECASE)
_RETRY_IN_SEC_RE = re.compile(r'try\s+again\s+in\s+~?(\d+)\s*(?:sec|second|s)\b', re.IGNORECASE)
_RESETS_AT_EPOCH_RE = re.compile(r'"?resets?_?at"?\s*[:=]\s*(\d{10})', re.IGNORECASE)


def is_quota_limit_error(error_text: str, engine: str = '') -> bool:
    """Return True if error_text indicates a subscription usage-limit exhaustion."""
    if not error_text:
        return False
    if engine and engine not in SUBSCRIPTION_ENGINES:
        # Custom HTTP API keys may hit per-minute RPM limits; do not auto-wait indefinitely.
        return False
    return any(pat.search(error_text) for pat in _QUOTA_ERROR_PATTERNS)


def resolve_reset_timestamp(
    engine: str,
    model: Optional[str],
    error_text: str,
    quotas_snapshot: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    """Resolve the earliest trustworthy quota reset epoch timestamp and its source."""
    cur_now = now if now is not None else time.time()

    # 1. Explicit epoch in structured error text
    m_epoch = _RESETS_AT_EPOCH_RE.search(error_text or '')
    if m_epoch:
        ts = float(int(m_epoch.group(1)))
        if ts > cur_now:
            return ts, 'error_epoch'

    # 2. Relative duration in error text
    m_min = _RETRY_IN_MIN_RE.search(error_text or '')
    if m_min:
        mins = max(1, int(m_min.group(1)))
        return cur_now + mins * 60, 'error_relative_mins'

    m_sec = _RETRY_IN_SEC_RE.search(error_text or '')
    if m_sec:
        secs = max(10, int(m_sec.group(1)))
        return cur_now + secs, 'error_relative_secs'

    # 3. Exhausted window in quotas_snapshot
    if isinstance(quotas_snapshot, dict):
        if engine == 'codex':
            codex = quotas_snapshot.get('codex') or {}
            candidates = []
            for key in ('primary', 'secondary'):
                w = codex.get(key)
                if isinstance(w, dict) and w.get('resets_at'):
                    rem = w.get('remaining_percent')
                    used = w.get('used_percent')
                    if (rem is not None and rem <= 1) or (used is not None and used >= 99):
                        if float(w['resets_at']) > cur_now:
                            candidates.append(float(w['resets_at']))
            if candidates:
                return max(candidates), 'codex_quota_window'
        elif engine == 'antigravity':
            agy = quotas_snapshot.get('antigravity') or {}
            buckets = agy.get('buckets') or []
            candidates = []
            for b in buckets:
                if not isinstance(b, dict):
                    continue
                rem = b.get('remaining_percent')
                ts = float(b.get('resets_at') or 0)
                if rem is not None and rem <= 1 and ts > cur_now:
                    candidates.append(ts)
            if candidates:
                return min(candidates), 'antigravity_quota_bucket'

    return None, ''


def _stagger_jitter(session_id: str) -> float:
    digest = hashlib.sha256(session_id.encode('utf-8')).digest()
    return 10.0 + (digest[0] % 25)


def schedule_quota_wait(
    session_id: str,
    job_id: str,
    engine: str,
    model: Optional[str],
    prompt: str,
    has_output: bool,
    error_text: str,
    quotas_snapshot: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Persist an auto-continue wait entry if eligible. Returns the row or None."""
    if not is_quota_limit_error(error_text, engine=engine):
        return None
    cur_now = now if now is not None else time.time()
    reset_at, source = resolve_reset_timestamp(
        engine=engine,
        model=model,
        error_text=error_text,
        quotas_snapshot=quotas_snapshot,
        now=cur_now,
    )
    if not reset_at or reset_at <= cur_now:
        return None

    # Count consecutive auto-continues without substantive progress
    prev = db.one(
        'SELECT attempts FROM quota_waits WHERE session_id=? ORDER BY created DESC LIMIT 1',
        (session_id,),
    )
    attempts = int(prev['attempts']) if prev else 0
    if attempts >= MAX_AUTO_CONTINUE_ATTEMPTS:
        return None

    resume_at = reset_at + DEFAULT_BUFFER_SECS + _stagger_jitter(session_id)
    ident = secrets.token_hex(10)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE quota_waits SET status='cancelled', updated=? WHERE session_id=? AND status='waiting'",
            (cur_now, session_id),
        )
        conn.execute(
            'INSERT INTO quota_waits(id, session_id, job_id, engine, model, prompt, has_output, '
            'reset_at, resume_at, attempts, status, reason, created, updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                ident,
                session_id,
                job_id,
                engine,
                model or '',
                prompt,
                1 if has_output else 0,
                reset_at,
                resume_at,
                attempts + 1,
                'waiting',
                source,
                cur_now,
                cur_now,
            ),
        )
    return db.one('SELECT * FROM quota_waits WHERE id=?', (ident,))


def reset_attempts(session_id: str) -> None:
    """Reset consecutive auto-continue counter after a successful turn."""
    db.execute(
        "UPDATE quota_waits SET attempts=0, updated=? WHERE session_id=? AND status IN ('resumed','completed')",
        (time.time(), session_id),
    )


def get_active_quota_wait(session_id: str) -> Optional[Dict[str, Any]]:
    return db.one(
        "SELECT * FROM quota_waits WHERE session_id=? AND status='waiting' ORDER BY created DESC LIMIT 1",
        (session_id,),
    )


def cancel_quota_wait(session_id: str, reason: str = 'user_cancelled') -> int:
    """Cancel any pending quota wait for a session (on user message, model switch, or cancel)."""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE quota_waits SET status='cancelled', reason=?, updated=? "
            "WHERE session_id=? AND status='waiting'",
            (reason, time.time(), session_id),
        )
        return cur.rowcount


def process_due_quota_waits(
    resume_fn: Callable[[Dict[str, Any]], bool],
    now: Optional[float] = None,
) -> int:
    """Claim and dispatch all due quota waits. Returns count of resumed sessions."""
    cur_now = now if now is not None else time.time()
    due = db.query(
        "SELECT * FROM quota_waits WHERE status='waiting' AND resume_at <= ? ORDER BY resume_at ASC",
        (cur_now,),
    )
    resumed = 0
    for row in due:
        # Claim atomically so concurrent ticks/workers never double-dispatch
        with db.transaction() as conn:
            active_job = conn.execute(
                "SELECT id FROM jobs WHERE session_id=? AND status IN ('queued','running','waiting') LIMIT 1",
                (row['session_id'],),
            ).fetchone()
            if active_job:
                continue
            claimed = conn.execute(
                "UPDATE quota_waits SET status='resuming', updated=? WHERE id=? AND status='waiting'",
                (cur_now, row['id']),
            )
            if not claimed.rowcount:
                continue
        ok = False
        try:
            ok = bool(resume_fn(row))
        except Exception:
            ok = False
        db.execute(
            'UPDATE quota_waits SET status=?, updated=? WHERE id=?',
            ('resumed' if ok else 'failed', time.time(), row['id']),
        )
        if ok:
            resumed += 1
    return resumed
