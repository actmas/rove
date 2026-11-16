"""Session Fork (Side Chat) support at any job/turn boundary.

Inspired by makecindy/cindy's side_chat / fork provenance:
- Forks an existing session at the end (or at a specific `target_job_id`),
  copying event history up to that boundary into a fresh session with
  `parent_session_id` and `forked_at_job_id` provenance.
- Clears native_id on the fork so the forked branch starts a clean native
  thread seeded via context_bridge without mutating the parent thread.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional

from . import store as db


def fork_session_at_job(
    source_session_id: str,
    target_job_id: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a forked child session copying events up to target_job_id (inclusive)."""
    src = db.one('SELECT * FROM sessions WHERE id=?', (source_session_id,))
    if not src:
        raise ValueError('源会话不存在')

    all_events = db.query(
        'SELECT job_id, kind, payload, created FROM events WHERE session_id=? ORDER BY id ASC',
        (source_session_id,),
    )
    if target_job_id:
        cutoff_idx = -1
        for i, ev in enumerate(all_events):
            if ev.get('job_id') == target_job_id:
                cutoff_idx = i
        if cutoff_idx < 0:
            raise ValueError('未找到指定的分叉轮次 (job_id)')
        copied_events = all_events[:cutoff_idx + 1]
    else:
        copied_events = all_events

    new_id = secrets.token_hex(12)
    now = time.time()
    new_title = (title or '').strip() or f"{src.get('title') or '会话'} (分支)"
    with db.transaction() as conn:
        conn.execute(
            'INSERT INTO sessions(id, project_id, engine, title, native_id, created, updated, '
            'model, effort, mode, fast, pinned, cwd, parent_session_id, forked_at_job_id, last_engine) '
            'VALUES(?,?,?,?,NULL,?,?,?,?,0,0,?,?,?,?,?)',
            (
                new_id,
                src['project_id'],
                src.get('engine') or 'codex',
                new_title,
                now,
                now,
                src.get('model'),
                src.get('effort'),
                src.get('mode') or 'standard',
                src.get('cwd'),
                source_session_id,
                target_job_id,
                src.get('last_engine') or src.get('engine'),
            ),
        )
        for ev in copied_events:
            conn.execute(
                'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
                (new_id, ev['job_id'], ev['kind'], ev['payload'], ev['created']),
            )
    return db.one('SELECT * FROM sessions WHERE id=?', (new_id,))
