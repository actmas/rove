"""SQLite storage lifecycle, diagnostic metrics, safe event compaction and retention management."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import sqlite3
import time

from . import store as db


def _format_bytes(num: int | float) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != 'B' else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TB"


def get_storage_stats() -> dict:
    """Collect accurate storage statistics, table page usage, and retention diagnostic counts."""
    db.init()
    db_file = db.DATA / 'state.sqlite3'
    wal_file = db.DATA / 'state.sqlite3-wal'
    shm_file = db.DATA / 'state.sqlite3-shm'

    db_size = db_file.stat().st_size if db_file.exists() else 0
    wal_size = wal_file.stat().st_size if wal_file.exists() else 0
    shm_size = shm_file.stat().st_size if shm_file.exists() else 0
    total_file_size = db_size + wal_size + shm_size

    now = time.time()
    cutoff_7d = now - 7 * 86400
    cutoff_14d = now - 14 * 86400
    cutoff_30d = now - 30 * 86400

    table_stats = {}
    dbstat_map = {}

    with contextlib.closing(db.connect()) as conn:
        with contextlib.suppress(Exception):
            for row in conn.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall():
                dbstat_map[row[0]] = row[1]

        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for t in tables:
            cnt = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            byte_size = dbstat_map.get(t, 0)
            if byte_size == 0 and cnt > 0:
                with contextlib.suppress(Exception):
                    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                    if cols:
                        expr = ' + '.join(f'length(coalesce("{c}", ""))' for c in cols)
                        byte_size = conn.execute(f"SELECT sum({expr}) FROM {t}").fetchone()[0] or 0
            table_stats[t] = {
                'count': cnt,
                'bytes': byte_size,
                'size_formatted': _format_bytes(byte_size),
            }

        total_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        pinned_sessions = conn.execute("SELECT count(*) FROM sessions WHERE pinned = 1").fetchone()[0]
        active_jobs = conn.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
        ).fetchone()[0]

        unpinned_old_7d = conn.execute(
            "SELECT count(*) FROM sessions WHERE updated < ? AND pinned = 0", (cutoff_7d,)
        ).fetchone()[0]
        unpinned_old_14d = conn.execute(
            "SELECT count(*) FROM sessions WHERE updated < ? AND pinned = 0", (cutoff_14d,)
        ).fetchone()[0]
        unpinned_old_30d = conn.execute(
            "SELECT count(*) FROM sessions WHERE updated < ? AND pinned = 0", (cutoff_30d,)
        ).fetchone()[0]

        codex_deltas = conn.execute("""
            SELECT count(*) FROM events
            WHERE kind = 'codex'
              AND json_extract(payload, '$.method') IN (
                'item/agentMessage/delta',
                'item/commandExecution/outputDelta',
                'item/reasoning/textDelta',
                'item/reasoning/summaryTextDelta',
                'item/plan/delta'
              )
        """).fetchone()[0]

        agy_uncompleted = conn.execute("""
            SELECT count(*) FROM events
            WHERE kind = 'antigravity'
              AND (
                (json_extract(payload, '$.event') = 'agy_step' AND (json_extract(payload, '$.completed') = 0 OR json_extract(payload, '$.completed') = false))
                OR json_extract(payload, '$.event') = 'step_update'
              )
        """).fetchone()[0]

        large_images = conn.execute("""
            SELECT count(*) FROM events
            WHERE kind = 'codex'
              AND json_extract(payload, '$.params.item.type') = 'imageGeneration'
              AND length(payload) > 10000
        """).fetchone()[0]

        expired_logins = conn.execute("SELECT count(*) FROM logins WHERE expires < ?", (now,)).fetchone()[0]
        old_terminals = conn.execute(
            "SELECT count(*) FROM terminals WHERE status != 'running' AND created < ?", (cutoff_7d,)
        ).fetchone()[0]

    return {
        'db_path': str(db_file),
        'db_size_bytes': db_size,
        'wal_size_bytes': wal_size,
        'total_size_bytes': total_file_size,
        'total_size_formatted': _format_bytes(total_file_size),
        'db_size_formatted': _format_bytes(db_size),
        'wal_size_formatted': _format_bytes(wal_size),
        'tables': table_stats,
        'diagnostics': {
            'total_sessions': total_sessions,
            'pinned_sessions': pinned_sessions,
            'active_jobs': active_jobs,
            'unpinned_old_7d': unpinned_old_7d,
            'unpinned_old_14d': unpinned_old_14d,
            'unpinned_old_30d': unpinned_old_30d,
            'compactable_codex_deltas': codex_deltas,
            'compactable_agy_steps': agy_uncompleted,
            'compactable_image_blobs': large_images,
            'compactable_events_total': codex_deltas + agy_uncompleted + large_images,
            'expired_logins': expired_logins,
            'old_exited_terminals': old_terminals,
        },
    }


def create_backup(label: str = 'lifecycle') -> str:
    """Create a consistent online SQLite backup before performing destructive maintenance."""
    db.init()
    target_dir = db.DATA / 'backups'
    target_dir.mkdir(exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_file = target_dir / f"state-{stamp}-{label}.sqlite3"

    with contextlib.closing(db.connect()) as source, contextlib.closing(sqlite3.connect(backup_file)) as destination:
        source.backup(destination)
    os.chmod(backup_file, 0o600)
    return str(backup_file)


def compact_old_events(cutoff_timestamp: float | None = None, dry_run: bool = False, keep_pinned: bool = True) -> dict:
    """
    Safely compacts intermediate streaming deltas and bloated inline blobs from completed sessions.

    Guarantees:
    - Never touches sessions that have active jobs (running, queued, waiting).
    - Preserves pinned sessions by default (keep_pinned=True).
    - Preserves all user prompts (kind='user'), notices, job statuses, and final agent messages/results.
    - If cutoff_timestamp is None, compacts all finished non-active sessions.
    """
    db.init()
    with contextlib.closing(db.connect()) as conn:
        active_jobs_rows = conn.execute(
            "SELECT id, session_id FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
        ).fetchall()
        active_job_ids = {r[0] for r in active_jobs_rows}
        active_sids = {r[1] for r in active_jobs_rows}

        query = "SELECT id, updated, pinned FROM sessions"
        all_sessions = conn.execute(query).fetchall()

        eligible_sids = []
        for sid, updated, pinned in all_sessions:
            if keep_pinned and pinned:
                continue
            if cutoff_timestamp is not None and updated >= cutoff_timestamp:
                continue
            eligible_sids.append(sid)

        if not eligible_sids:
            return {
                'eligible_sessions': 0,
                'codex_deltas_deleted': 0,
                'agy_steps_deleted': 0,
                'images_compacted': 0,
                'total_events_pruned': 0,
            }

        s_placeholders = ','.join('?' for _ in eligible_sids)
        scope_params: list = list(eligible_sids)
        if active_job_ids:
            j_placeholders = ','.join('?' for _ in active_job_ids)
            s_act_placeholders = ','.join('?' for _ in active_sids)
            scope_sql = (
                f"session_id IN ({s_placeholders}) "
                f"AND ((job_id IS NOT NULL AND job_id NOT IN ({j_placeholders})) "
                f"OR (job_id IS NULL AND session_id NOT IN ({s_act_placeholders})))"
            )
            scope_params.extend(active_job_ids)
            scope_params.extend(active_sids)
        else:
            scope_sql = f"session_id IN ({s_placeholders})"

        # 1. Codex streaming deltas
        codex_deltas_count = conn.execute(f"""
            SELECT count(*) FROM events
            WHERE {scope_sql}
              AND kind = 'codex'
              AND json_extract(payload, '$.method') IN (
                'item/agentMessage/delta',
                'item/commandExecution/outputDelta',
                'item/reasoning/textDelta',
                'item/reasoning/summaryTextDelta',
                'item/plan/delta'
              )
        """, scope_params).fetchone()[0]

        # 2. Antigravity intermediate uncompleted steps & streaming step_update
        agy_steps_count = conn.execute(f"""
            SELECT count(*) FROM events
            WHERE {scope_sql}
              AND kind = 'antigravity'
              AND (
                (json_extract(payload, '$.event') = 'agy_step' AND (json_extract(payload, '$.completed') = 0 OR json_extract(payload, '$.completed') = false))
                OR json_extract(payload, '$.event') = 'step_update'
              )
        """, scope_params).fetchone()[0]

        # 3. Large inline imageGeneration blobs
        image_rows = conn.execute(f"""
            SELECT id, payload FROM events
            WHERE {scope_sql}
              AND kind = 'codex'
              AND json_extract(payload, '$.params.item.type') = 'imageGeneration'
              AND length(payload) > 10000
        """, scope_params).fetchall()

        images_to_compact = []
        for eid, p_raw in image_rows:
            try:
                p = json.loads(p_raw)
                item = (p.get('params') or {}).get('item') or {}
                res = item.get('result') or ''
                if (isinstance(res, str) and (res.startswith('data:image/') or len(res) > 5000)
                        and item.get('savedPath') and Path(item['savedPath']).is_file()):
                    item['result'] = '[embedded base64 archived; see savedPath]'
                    images_to_compact.append((eid, json.dumps(p, ensure_ascii=False)))
            except Exception:
                pass

        if dry_run:
            return {
                'dry_run': True,
                'eligible_sessions': len(eligible_sids),
                'codex_deltas_deleted': codex_deltas_count,
                'agy_steps_deleted': agy_steps_count,
                'images_compacted': len(images_to_compact),
                'total_events_pruned': codex_deltas_count + agy_steps_count,
            }

        # Record pruned_through_seq watermark per session before deleting fragmented deltas
        pruned_watermarks = conn.execute(f"""
            SELECT session_id, MAX(id) FROM events
            WHERE {scope_sql}
              AND (
                (kind = 'codex' AND json_extract(payload, '$.method') IN (
                    'item/agentMessage/delta',
                    'item/commandExecution/outputDelta',
                    'item/reasoning/textDelta',
                    'item/reasoning/summaryTextDelta',
                    'item/plan/delta'
                ))
                OR (kind = 'antigravity' AND (
                    (json_extract(payload, '$.event') = 'agy_step' AND (json_extract(payload, '$.completed') = 0 OR json_extract(payload, '$.completed') = false))
                    OR json_extract(payload, '$.event') = 'step_update'
                ))
              )
            GROUP BY session_id
        """, scope_params).fetchall()
        for sid, max_pruned_id in pruned_watermarks:
            if max_pruned_id:
                conn.execute(
                    "UPDATE sessions SET pruned_through_seq = MAX(COALESCE(pruned_through_seq, 0), ?) WHERE id = ?",
                    (max_pruned_id, sid),
                )

        # Execute mutations
        conn.execute(f"""
            DELETE FROM events
            WHERE {scope_sql}
              AND kind = 'codex'
              AND json_extract(payload, '$.method') IN (
                'item/agentMessage/delta',
                'item/commandExecution/outputDelta',
                'item/reasoning/textDelta',
                'item/reasoning/summaryTextDelta',
                'item/plan/delta'
              )
        """, scope_params)

        conn.execute(f"""
            DELETE FROM events
            WHERE {scope_sql}
              AND kind = 'antigravity'
              AND (
                (json_extract(payload, '$.event') = 'agy_step' AND (json_extract(payload, '$.completed') = 0 OR json_extract(payload, '$.completed') = false))
                OR json_extract(payload, '$.event') = 'step_update'
              )
        """, scope_params)

        for eid, new_payload in images_to_compact:
            conn.execute("UPDATE events SET payload = ? WHERE id = ?", (new_payload, eid))

        conn.commit()

        return {
            'dry_run': False,
            'eligible_sessions': len(eligible_sids),
            'codex_deltas_deleted': codex_deltas_count,
            'agy_steps_deleted': agy_steps_count,
            'images_compacted': len(images_to_compact),
            'total_events_pruned': codex_deltas_count + agy_steps_count,
        }


def prune_sessions(cutoff_timestamp: float, dry_run: bool = False, keep_pinned: bool = True) -> dict:
    """
    Permanently purges old completed sessions and their associated logs, events, and approvals.

    Guarantees:
    - Never prunes sessions with running/queued/waiting jobs.
    - Preserves pinned sessions by default.
    - Preserves project configurations and project memories.
    """
    db.init()
    with contextlib.closing(db.connect()) as conn:
        active_sids = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT session_id FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchall()
        }

        query = "SELECT id, pinned FROM sessions WHERE updated < ?"
        rows = conn.execute(query, (cutoff_timestamp,)).fetchall()

        target_sids = []
        for sid, pinned in rows:
            if sid in active_sids:
                continue
            if keep_pinned and pinned:
                continue
            target_sids.append(sid)

        if not target_sids:
            return {
                'pruned_sessions': 0,
                'pruned_events': 0,
                'pruned_jobs': 0,
                'pruned_terminals': 0,
            }

        placeholders = ','.join('?' for _ in target_sids)

        events_count = conn.execute(f"SELECT count(*) FROM events WHERE session_id IN ({placeholders})", target_sids).fetchone()[0]
        jobs_count = conn.execute(f"SELECT count(*) FROM jobs WHERE session_id IN ({placeholders})", target_sids).fetchone()[0]
        terminals_count = conn.execute(f"SELECT count(*) FROM terminals WHERE session_id IN ({placeholders})", target_sids).fetchone()[0]

        if dry_run:
            return {
                'dry_run': True,
                'pruned_sessions': len(target_sids),
                'pruned_events': events_count,
                'pruned_jobs': jobs_count,
                'pruned_terminals': terminals_count,
            }

        # Delete dependent records in strict dependency order
        conn.execute(
            f"DELETE FROM terminal_output WHERE terminal_id IN (SELECT id FROM terminals WHERE session_id IN ({placeholders}))",
            target_sids,
        )
        conn.execute(
            f"DELETE FROM terminal_input WHERE terminal_id IN (SELECT id FROM terminals WHERE session_id IN ({placeholders}))",
            target_sids,
        )
        conn.execute(f"DELETE FROM terminals WHERE session_id IN ({placeholders})", target_sids)
        conn.execute(
            f"DELETE FROM approvals WHERE job_id IN (SELECT id FROM jobs WHERE session_id IN ({placeholders}))",
            target_sids,
        )
        conn.execute(f"DELETE FROM jobs WHERE session_id IN ({placeholders})", target_sids)
        conn.execute(f"DELETE FROM events WHERE session_id IN ({placeholders})", target_sids)
        conn.execute(f"DELETE FROM synced_codex_items WHERE session_id IN ({placeholders})", target_sids)
        conn.execute(
            f"UPDATE project_memories SET source_session_id = NULL WHERE source_session_id IN ({placeholders})",
            target_sids,
        )
        conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", target_sids)
        conn.commit()

        return {
            'dry_run': False,
            'pruned_sessions': len(target_sids),
            'pruned_events': events_count,
            'pruned_jobs': jobs_count,
            'pruned_terminals': terminals_count,
        }


def prune_ephemeral(dry_run: bool = False) -> dict:
    """Prunes expired logins, old exited terminal outputs (>7 days), and orphaned rows."""
    db.init()
    now = time.time()
    cutoff_7d = now - 7 * 86400

    with contextlib.closing(db.connect()) as conn:
        expired_logins_count = conn.execute("SELECT count(*) FROM logins WHERE expires < ?", (now,)).fetchone()[0]
        old_term_outs = conn.execute("""
            SELECT count(*) FROM terminal_output
            WHERE terminal_id IN (SELECT id FROM terminals WHERE status != 'running' AND created < ?)
        """, (cutoff_7d,)).fetchone()[0]

        orphaned_events = conn.execute("""
            SELECT count(*) FROM events WHERE session_id NOT IN (SELECT id FROM sessions)
        """).fetchone()[0]

        if dry_run:
            return {
                'dry_run': True,
                'expired_logins': expired_logins_count,
                'old_terminal_outputs': old_term_outs,
                'orphaned_events': orphaned_events,
            }

        conn.execute("DELETE FROM logins WHERE expires < ?", (now,))
        conn.execute("""
            DELETE FROM terminal_output
            WHERE terminal_id IN (SELECT id FROM terminals WHERE status != 'running' AND created < ?)
        """, (cutoff_7d,))
        conn.execute("""
            DELETE FROM terminal_input
            WHERE terminal_id IN (SELECT id FROM terminals WHERE status != 'running' AND created < ?)
        """, (cutoff_7d,))
        conn.execute("DELETE FROM events WHERE session_id NOT IN (SELECT id FROM sessions)")
        conn.commit()

        return {
            'dry_run': False,
            'expired_logins': expired_logins_count,
            'old_terminal_outputs': old_term_outs,
            'orphaned_events': orphaned_events,
        }


def vacuum_database() -> dict:
    """Executes SQLite incremental/full VACUUM and WAL truncation to release disk space back to OS."""
    db.init()
    db_file = db.DATA / 'state.sqlite3'
    wal_file = db.DATA / 'state.sqlite3-wal'

    size_before = (db_file.stat().st_size if db_file.exists() else 0) + (wal_file.stat().st_size if wal_file.exists() else 0)

    with contextlib.closing(db.connect()) as conn:
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA optimize")

    size_after = (db_file.stat().st_size if db_file.exists() else 0) + (wal_file.stat().st_size if wal_file.exists() else 0)
    freed = max(0, size_before - size_after)

    return {
        'size_before_bytes': size_before,
        'size_after_bytes': size_after,
        'size_before_formatted': _format_bytes(size_before),
        'size_after_formatted': _format_bytes(size_after),
        'freed_bytes': freed,
        'freed_formatted': _format_bytes(freed),
    }


def run_lifecycle_maintenance(options: dict | None = None) -> dict:
    """
    Unified entry point for database health check, backup, compaction, pruning, and vacuum.
    """
    opts = options or {}
    keep_days = opts.get('keep_days', 14)
    compact_events = opts.get('compact_events', True)
    prune_old_sessions = opts.get('prune_sessions', False)
    vacuum = opts.get('vacuum', True)
    backup_before = opts.get('backup_before', False)
    dry_run = opts.get('dry_run', False)

    now = time.time()
    session_cutoff = now - (keep_days * 86400) if keep_days and keep_days > 0 else None
    compact_cutoff = None if opts.get('compact_all', True) else session_cutoff

    stats_before = get_storage_stats()
    backup_path = None

    if backup_before and not dry_run:
        backup_path = create_backup(label='before-prune')

    ephemeral_res = prune_ephemeral(dry_run=dry_run)

    compact_res = {}
    if compact_events:
        compact_res = compact_old_events(cutoff_timestamp=compact_cutoff, dry_run=dry_run)

    sessions_res = {}
    if prune_old_sessions and session_cutoff:
        sessions_res = prune_sessions(cutoff_timestamp=session_cutoff, dry_run=dry_run)

    vacuum_res = {}
    if vacuum and not dry_run:
        vacuum_res = vacuum_database()

    stats_after = get_storage_stats()

    size_before = stats_before['total_size_bytes']
    size_after = stats_after['total_size_bytes']
    total_freed = max(0, size_before - size_after)

    return {
        'ok': True,
        'dry_run': dry_run,
        'backup_path': backup_path,
        'keep_days': keep_days,
        'ephemeral': ephemeral_res,
        'compact': compact_res,
        'sessions': sessions_res,
        'vacuum': vacuum_res,
        'size_before_formatted': _format_bytes(size_before),
        'size_after_formatted': _format_bytes(size_after),
        'freed_bytes': total_freed,
        'freed_formatted': _format_bytes(total_freed),
        'events_before': stats_before['tables'].get('events', {}).get('count', 0),
        'events_after': stats_after['tables'].get('events', {}).get('count', 0),
        'stats': stats_after,
    }
