"""Transactional SQLite persistence & CAS state operations for Multi-Agent Orchestration."""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from . import orchestration_schema as schema
from . import store as db


def _now() -> float:
    return time.time()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    return db.one('SELECT * FROM orchestration_runs WHERE id=?', (run_id,))


def get_run_by_request(request_id: str) -> Optional[Dict[str, Any]]:
    return db.one('SELECT * FROM orchestration_runs WHERE request_id=?', (request_id,))


def list_session_runs(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = db.query(
        'SELECT * FROM orchestration_runs WHERE root_session_id=? ORDER BY created DESC LIMIT ?',
        (session_id, limit),
    )
    return [enrich_run_summary(r) for r in rows]


def enrich_run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(run)
    for json_col, target in (('policy_json', 'policy'), ('budget_json', 'budget')):
        try:
            out[target] = json.loads(out.get(json_col) or '{}')
        except Exception:
            out[target] = {}
    steps = db.query(
        'SELECT * FROM orchestration_steps WHERE run_id=? AND plan_version=? ORDER BY step_index ASC',
        (run['id'], run['plan_version']),
    )
    attempts = db.query(
        'SELECT * FROM orchestration_attempts WHERE run_id=? ORDER BY started ASC',
        (run['id'],),
    )
    attempts_by_step: Dict[str, List[Dict[str, Any]]] = {}
    for att in attempts:
        attempts_by_step.setdefault(att['step_id'], []).append(att)
    for st in steps:
        for jc, tc in (
            ('depends_on_json', 'depends_on'),
            ('files_scope_json', 'files_scope'),
            ('acceptance_json', 'acceptance'),
            ('required_capabilities_json', 'required_capabilities'),
        ):
            try:
                st[tc] = json.loads(st.get(jc) or '[]')
            except Exception:
                st[tc] = []
        st['attempts'] = attempts_by_step.get(st['id'], [])
        st['latest_attempt'] = st['attempts'][-1] if st['attempts'] else None
    out['steps'] = steps
    out['artifacts'] = db.query(
        'SELECT * FROM orchestration_artifacts WHERE run_id=? ORDER BY created ASC',
        (run['id'],),
    )
    return out


def create_run_idempotent(
    *,
    request_id: str,
    project_id: str,
    root_session_id: str,
    prompt: str,
    strategy: str,
    policy: Dict[str, Any],
    budget: Dict[str, Any],
    baseline_ref: str,
    workspace_path: str,
    initial_status: str = 'planning',
) -> Tuple[Dict[str, Any], bool]:
    """Idempotently create an orchestration run + initial planner step. Returns (run, created)."""
    now = _now()
    run_id = 'orch_' + secrets.token_hex(8)
    step_id = 'step_' + secrets.token_hex(8)

    with db.transaction() as conn:
        existing = conn.execute(
            'SELECT * FROM orchestration_runs WHERE request_id=?', (request_id,)
        ).fetchone()
        if existing:
            return dict(existing), False

        conn.execute(
            'INSERT INTO orchestration_runs('
            'id, request_id, project_id, root_session_id, prompt, strategy, status, '
            'plan_version, revision, policy_json, budget_json, baseline_ref, workspace_path, '
            'fix_rounds, total_attempts, plan_retries, generation, created, updated'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                run_id,
                request_id,
                project_id,
                root_session_id,
                prompt,
                strategy,
                initial_status,
                1,
                1,
                json.dumps(policy, ensure_ascii=False),
                json.dumps(budget, ensure_ascii=False),
                baseline_ref,
                workspace_path,
                0,
                0,
                0,
                1,
                now,
                now,
            ),
        )
        conn.execute(
            'UPDATE sessions SET active_orch_run_id=?, updated=? WHERE id=?',
            (run_id, now, root_session_id),
        )
        if initial_status == 'planning':
            conn.execute(
                'INSERT INTO orchestration_steps('
                'id, run_id, plan_version, step_index, logical_key, title, role, status, '
                'depends_on_json, files_scope_json, acceptance_json, required_capabilities_json, '
                'prompt, optional, created, updated'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    step_id,
                    run_id,
                    1,
                    0,
                    'planner',
                    '架构拆解与方案规划',
                    'planner',
                    'ready',
                    '[]',
                    '[]',
                    '[]',
                    json.dumps(['repo_read', 'read_only_enforced']),
                    prompt,
                    0,
                    now,
                    now,
                ),
            )
        row = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        return dict(row), True


def insert_plan_steps(
    run_id: str,
    plan_version: int,
    steps: List[Dict[str, Any]],
    start_index: int = 1,
) -> List[Dict[str, Any]]:
    """Insert validated implementation steps for a run."""
    now = _now()
    inserted: List[Dict[str, Any]] = []
    with db.transaction() as conn:
        for offset, st in enumerate(steps):
            step_id = 'step_' + secrets.token_hex(8)
            deps = st.get('depends_on') or []
            status = 'ready' if not deps else 'pending'
            conn.execute(
                'INSERT OR IGNORE INTO orchestration_steps('
                'id, run_id, plan_version, step_index, logical_key, title, role, status, '
                'depends_on_json, files_scope_json, acceptance_json, required_capabilities_json, '
                'prompt, optional, created, updated'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    step_id,
                    run_id,
                    plan_version,
                    start_index + offset,
                    st['id'],
                    st['title'],
                    st['role'],
                    status,
                    json.dumps(deps, ensure_ascii=False),
                    json.dumps(st.get('files_scope') or [], ensure_ascii=False),
                    json.dumps(st.get('acceptance') or [], ensure_ascii=False),
                    json.dumps(st.get('required_capabilities') or ['repo_read', 'repo_write'], ensure_ascii=False),
                    st['objective'],
                    1 if st.get('optional') else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                'SELECT * FROM orchestration_steps WHERE run_id=? AND plan_version=? AND logical_key=?',
                (run_id, plan_version, st['id']),
            ).fetchone()
            if row:
                inserted.append(dict(row))
    return inserted


def insert_dynamic_step(
    run_id: str,
    plan_version: int,
    logical_key: str,
    title: str,
    role: str,
    prompt: str,
    depends_on: Optional[List[str]] = None,
    required_capabilities: Optional[List[str]] = None,
    files_scope: Optional[List[str]] = None,
    preferred_engine: Optional[str] = None,
    preferred_model: Optional[str] = None,
    preferred_effort: Optional[str] = None,
) -> Dict[str, Any]:
    now = _now()
    step_id = 'step_' + secrets.token_hex(8)
    with db.transaction() as conn:
        max_row = conn.execute(
            'SELECT COALESCE(MAX(step_index), 0) AS mx FROM orchestration_steps WHERE run_id=? AND plan_version=?',
            (run_id, plan_version),
        ).fetchone()
        next_idx = int((max_row['mx'] if max_row else 0)) + 1
        conn.execute(
            'INSERT INTO orchestration_steps('
            'id, run_id, plan_version, step_index, logical_key, title, role, status, '
            'depends_on_json, files_scope_json, acceptance_json, required_capabilities_json, '
            'prompt, optional, preferred_engine, preferred_model, preferred_effort, created, updated'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                step_id,
                run_id,
                plan_version,
                next_idx,
                logical_key,
                title,
                role,
                'ready',
                json.dumps(depends_on or [], ensure_ascii=False),
                json.dumps(files_scope or [], ensure_ascii=False),
                '[]',
                json.dumps(required_capabilities or ['repo_read'], ensure_ascii=False),
                prompt,
                0,
                preferred_engine,
                preferred_model,
                preferred_effort,
                now,
                now,
            ),
        )
        row = conn.execute('SELECT * FROM orchestration_steps WHERE id=?', (step_id,)).fetchone()
        return dict(row)


def refresh_ready_steps(run_id: str, plan_version: int) -> List[Dict[str, Any]]:
    """Promote pending steps whose dependencies are all succeeded/skipped(optional), or block if required dep failed/skipped."""
    now = _now()
    with db.transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM orchestration_steps WHERE run_id=? AND plan_version=? ORDER BY step_index ASC',
            (run_id, plan_version),
        ).fetchall()]
        by_key = {r['logical_key']: r for r in rows}
        newly_ready: List[Dict[str, Any]] = []
        for st in rows:
            if st['status'] != 'pending':
                continue
            try:
                deps = json.loads(st.get('depends_on_json') or '[]')
            except Exception:
                deps = []
            blocked = False
            all_met = True
            for d_key in deps:
                dep = by_key.get(d_key)
                if not dep:
                    blocked = True
                    break
                if dep['status'] in ('failed', 'cancelled', 'blocked') or (dep['status'] == 'skipped' and not dep.get('optional')):
                    blocked = True
                    break
                if dep['status'] not in ('succeeded', 'skipped'):
                    all_met = False
            if blocked:
                conn.execute(
                    "UPDATE orchestration_steps SET status='blocked', updated=? WHERE id=? AND status='pending'",
                    (now, st['id']),
                )
            elif all_met:
                conn.execute(
                    "UPDATE orchestration_steps SET status='ready', updated=? WHERE id=? AND status='pending'",
                    (now, st['id']),
                )
                st['status'] = 'ready'
                newly_ready.append(st)
        return newly_ready


def claim_step_and_create_dispatch(
    *,
    run_id: str,
    step_id: str,
    engine: str,
    model: Optional[str],
    effort: Optional[str],
    mode: str,
    prompt_text: str,
    route_reason: str,
    capability_snapshot: Dict[str, Any],
    input_digest: str = '',
) -> Optional[Dict[str, Any]]:
    """Atomically claim a ready step, reserve budget, create sub-session/attempt/job/dispatch."""
    now = _now()
    attempt_id = 'att_' + secrets.token_hex(8)
    job_id = secrets.token_hex(12)

    with db.transaction() as conn:
        run = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return None
        if run['status'] in schema.RUN_TERMINAL_STATES or run['status'] in ('pausing', 'paused', 'cancelling', 'needs_attention'):
            return None

        budget = json.loads(run['budget_json'] or '{}')
        max_attempts = int(budget.get('max_attempts', schema.DEFAULT_MAX_ATTEMPTS))
        if int(run['total_attempts']) >= max_attempts:
            return None

        step = conn.execute('SELECT * FROM orchestration_steps WHERE id=?', (step_id,)).fetchone()
        if not step or step['status'] != 'ready':
            return None

        # Ensure an internal sub-session exists for this step
        sub_session_id = step['sub_session_id']
        if not sub_session_id:
            sub_session_id = secrets.token_hex(12)
            root_sess = conn.execute('SELECT * FROM sessions WHERE id=?', (run['root_session_id'],)).fetchone()
            cwd = run['workspace_path'] or (root_sess['cwd'] if root_sess else '')
            conn.execute(
                'INSERT INTO sessions('
                'id, project_id, engine, title, created, updated, model, effort, mode, fast, cwd, '
                'thread_kind, parent_session_id, orch_role, active_orch_run_id'
                ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    sub_session_id,
                    run['project_id'],
                    engine,
                    f"[{step['role']}] {step['title'][:40]}",
                    now,
                    now,
                    model,
                    effort,
                    mode,
                    0,
                    cwd,
                    'subagent',
                    run['root_session_id'],
                    step['role'],
                    run_id,
                ),
            )
            conn.execute(
                'UPDATE orchestration_steps SET sub_session_id=? WHERE id=?',
                (sub_session_id, step_id),
            )
        else:
            conn.execute(
                'UPDATE sessions SET engine=?, model=?, effort=?, mode=?, updated=? WHERE id=?',
                (engine, model, effort, mode, now, sub_session_id),
            )

        att_row = conn.execute(
            'SELECT COALESCE(MAX(attempt_no), 0) AS mx FROM orchestration_attempts WHERE step_id=?',
            (step_id,),
        ).fetchone()
        attempt_no = int(att_row['mx'] if att_row else 0) + 1
        generation = int(run['generation'])

        conn.execute(
            'INSERT INTO orchestration_attempts('
            'id, run_id, step_id, attempt_no, job_id, generation, engine, model, effort, '
            'route_reason, capability_snapshot_json, input_digest, started'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                attempt_id,
                run_id,
                step_id,
                attempt_no,
                job_id,
                generation,
                engine,
                model,
                effort,
                route_reason,
                json.dumps(capability_snapshot, ensure_ascii=False),
                input_digest,
                now,
            ),
        )
        conn.execute(
            'INSERT INTO jobs('
            'id, session_id, request_id, prompt, status, started, model, effort, mode, fast, images, engine, '
            'orch_run_id, orch_step_id, orch_attempt_id, orch_generation'
            ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                job_id,
                sub_session_id,
                f'orch-{attempt_id}',
                prompt_text,
                'queued',
                now,
                model,
                effort,
                mode,
                0,
                '[]',
                engine,
                run_id,
                step_id,
                attempt_id,
                generation,
            ),
        )
        conn.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (
                sub_session_id,
                job_id,
                'user',
                json.dumps({'text': prompt_text, 'engine': engine, 'model': model, 'effort': effort, 'mode': mode, 'orch_role': step['role']}, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            'INSERT INTO orchestration_dispatches(attempt_id, run_id, job_id, status, generation, created, updated) VALUES(?,?,?,?,?,?,?)',
            (attempt_id, run_id, job_id, 'pending', generation, now, now),
        )
        conn.execute(
            "UPDATE orchestration_steps SET status='dispatched', active_attempt_id=?, updated=? WHERE id=?",
            (attempt_id, now, step_id),
        )
        conn.execute(
            'UPDATE orchestration_runs SET total_attempts=total_attempts+1, revision=revision+1, updated=? WHERE id=?',
            (now, run_id),
        )
        return {
            'attempt_id': attempt_id,
            'job_id': job_id,
            'sub_session_id': sub_session_id,
            'run_id': run_id,
            'step_id': step_id,
            'generation': generation,
            'engine': engine,
            'model': model,
            'effort': effort,
            'mode': mode,
        }


def mark_dispatch_spawned(attempt_id: str, generation: int) -> None:
    db.execute(
        "UPDATE orchestration_dispatches SET status='spawned', updated=? WHERE attempt_id=? AND generation=? AND status='pending'",
        (_now(), attempt_id, generation),
    )


def record_attempt_completion_cas(
    *,
    attempt_id: str,
    generation: int,
    outcome: str,
    output_digest: str = '',
    error_class: str = '',
    error_message: str = '',
) -> Optional[Dict[str, Any]]:
    """CAS-guarded completion recorder. Returns None if duplicate or stale generation."""
    now = _now()
    step_status = 'succeeded' if outcome == 'completed' else ('cancelled' if outcome == 'stopped' else 'failed')
    with db.transaction() as conn:
        att = conn.execute('SELECT * FROM orchestration_attempts WHERE id=?', (attempt_id,)).fetchone()
        if not att:
            return None
        if int(att['generation']) != int(generation) or att['outcome'] is not None:
            return None
        upd = conn.execute(
            'UPDATE orchestration_attempts SET ended=?, outcome=?, output_digest=?, error_class=?, error_message=? '
            'WHERE id=? AND generation=? AND outcome IS NULL',
            (now, outcome, output_digest, error_class, (error_message or '')[:2000], attempt_id, generation),
        )
        if not upd.rowcount:
            return None
        conn.execute(
            "UPDATE orchestration_dispatches SET status='completed', updated=? WHERE attempt_id=?",
            (now, attempt_id),
        )
        conn.execute(
            'UPDATE orchestration_steps SET status=?, updated=? WHERE id=? AND active_attempt_id=?',
            (step_status, now, att['step_id'], attempt_id),
        )
        row = conn.execute('SELECT * FROM orchestration_attempts WHERE id=?', (attempt_id,)).fetchone()
        return dict(row)


def record_artifact(
    *,
    run_id: str,
    step_id: str,
    attempt_id: str,
    kind: str,
    location: str,
    content_hash: str,
    baseline_ref: str = '',
    summary: str = '',
    metadata: Optional[Dict[str, Any]] = None,
    verified: bool = True,
) -> Dict[str, Any]:
    now = _now()
    art_id = 'art_' + secrets.token_hex(8)
    db.execute(
        'INSERT INTO orchestration_artifacts('
        'id, run_id, step_id, attempt_id, kind, location, content_hash, baseline_ref, summary, metadata_json, verified, created'
        ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            art_id,
            run_id,
            step_id,
            attempt_id,
            kind,
            location,
            content_hash,
            baseline_ref,
            summary[:2000],
            json.dumps(metadata or {}, ensure_ascii=False),
            1 if verified else 0,
            now,
        ),
    )
    return db.one('SELECT * FROM orchestration_artifacts WHERE id=?', (art_id,))


def transition_run_status(
    run_id: str,
    new_status: str,
    *,
    wait_reason: str = '',
    terminal_reason: str = '',
    increment_fix_rounds: bool = False,
    increment_plan_retries: bool = False,
    clear_root_active: bool = False,
) -> Optional[Dict[str, Any]]:
    now = _now()
    with db.transaction() as conn:
        run = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return None
        fix_inc = 1 if increment_fix_rounds else 0
        plan_inc = 1 if increment_plan_retries else 0
        conn.execute(
            'UPDATE orchestration_runs SET status=?, wait_reason=?, terminal_reason=?, '
            'fix_rounds=fix_rounds+?, plan_retries=plan_retries+?, revision=revision+1, updated=? WHERE id=?',
            (new_status, wait_reason, terminal_reason, fix_inc, plan_inc, now, run_id),
        )
        if clear_root_active:
            conn.execute(
                'UPDATE sessions SET active_orch_run_id=NULL WHERE id=? AND active_orch_run_id=?',
                (run['root_session_id'], run_id),
            )
        row = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        return dict(row)


def cancel_run_atomic(run_id: str, reason: str = '用户取消协同任务') -> Optional[Dict[str, Any]]:
    """Atomically cancel an orchestration run, all pending/ready steps, dispatches, and sub-jobs."""
    now = _now()
    with db.transaction() as conn:
        run = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            return None
        if run['status'] in schema.RUN_TERMINAL_STATES:
            return dict(run)

        conn.execute(
            "UPDATE orchestration_runs SET status='cancelled', terminal_reason=?, generation=generation+1, revision=revision+1, updated=? WHERE id=?",
            (reason, now, run_id),
        )
        conn.execute(
            "UPDATE orchestration_steps SET status='cancelled', updated=? WHERE run_id=? AND status IN ('pending','ready','dispatched','running')",
            (now, run_id),
        )
        conn.execute(
            "UPDATE orchestration_dispatches SET status='cancelled', updated=? WHERE run_id=? AND status='pending'",
            (now, run_id),
        )
        # Cancel queued jobs immediately; signal running/waiting jobs to stop
        conn.execute(
            "UPDATE jobs SET status='stopped', error=?, ended=? WHERE orch_run_id=? AND status='queued'",
            (reason, now, run_id),
        )
        conn.execute(
            "UPDATE jobs SET stop=1 WHERE orch_run_id=? AND status IN ('running','waiting')",
            (run_id,),
        )
        # Cancel any quota_waits associated with sub-sessions of this run
        conn.execute(
            "UPDATE quota_waits SET status='cancelled', reason=?, updated=? "
            "WHERE session_id IN (SELECT sub_session_id FROM orchestration_steps WHERE run_id=? AND sub_session_id IS NOT NULL) "
            "AND status IN ('waiting','retrying')",
            (reason, now, run_id),
        )
        conn.execute(
            'UPDATE sessions SET active_orch_run_id=NULL WHERE id=? AND active_orch_run_id=?',
            (run['root_session_id'], run_id),
        )
        row = conn.execute('SELECT * FROM orchestration_runs WHERE id=?', (run_id,)).fetchone()
        return dict(row)
