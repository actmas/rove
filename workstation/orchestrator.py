"""Deterministic Multi-Agent Orchestrator Controller for Rove Workstation."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import agents
from . import context_bridge
from . import model_router
from . import orchestration_schema as schema
from . import orchestration_store as ostore
from . import quotas
from . import review
from . import store as db


def _emit_orch_event(root_session_id: str, run_id: str, event_type: str, data: Dict[str, Any]) -> None:
    db.event(
        root_session_id,
        None,
        'orchestration',
        {'event': event_type, 'run_id': run_id, **data},
    )


def should_orchestrate(
    prompt: str,
    strategy: Optional[str],
    mode: str = 'standard',
    has_images: bool = False,
) -> bool:
    strat = schema.normalize_strategy(strategy)
    if strat == 'off':
        return False
    if strat in ('relay', 'parallel'):
        return True
    # strat == 'auto': only auto-orchestrate complex tasks
    task_info = model_router.classify_task(prompt=prompt, mode=mode, has_images=has_images)
    return task_info.get('complexity') == 'complex'


def _extract_job_reply(sub_session_id: str, job_id: str) -> str:
    turns = context_bridge.extract_session_turns(sub_session_id)
    for t in reversed(turns):
        if t.get('job_id') == job_id and t.get('reply'):
            return str(t['reply']).strip()
    # Fallback: check raw events for text
    rows = db.query(
        'SELECT kind, payload FROM events WHERE session_id=? AND job_id=? ORDER BY id DESC LIMIT 30',
        (sub_session_id, job_id),
    )
    for row in rows:
        try:
            payload = json.loads(row.get('payload') or '{}')
        except Exception:
            continue
        if row['kind'] == 'antigravity' and payload.get('event') == 'result':
            res = payload.get('result') or payload
            if isinstance(res, dict) and (res.get('text') or res.get('response')):
                return str(res.get('text') or res.get('response')).strip()
    return ''


def build_planner_prompt(
    user_prompt: str,
    project: Dict[str, Any],
    strategy: str = 'relay',
    max_impl_steps: int = schema.MAX_IMPL_STEPS,
) -> str:
    return (
        f"# [多智能体总控规划阶段 / Orchestrator Planner]\n"
        f"- **项目**: `{project.get('name', '当前项目')}` (`{project.get('path', '')}`)\n"
        f"- **模式**: 只读架构分析与任务拆解（严禁修改任何文件）\n"
        f"- **协同策略**: `{strategy}`（最多 {max_impl_steps} 个实现步骤）\n\n"
        f"## 用户目标\n{user_prompt.strip()}\n\n"
        "## 输出协议要求\n"
        "1. 先用简洁中文给出分析结论与各阶段分工方案。\n"
        "2. 在回答最后**必须**附带唯一一个严格合法的 JSON 计划块（使用以下标签包裹，不得嵌套多个）：\n"
        "<!-- ROVE_PLAN_START -->\n"
        "{\n"
        '  "schema_version": 1,\n'
        f'  "strategy": "{strategy}",\n'
        '  "require_cross_review": true,\n'
        '  "steps": [\n'
        '    {\n'
        '      "id": "step_1",\n'
        '      "title": "步骤简短标题",\n'
        '      "role": "coder_backend | coder_frontend | coder_fast | verifier",\n'
        '      "depends_on": [],\n'
        '      "files_scope": ["relative/path/file.py"],\n'
        '      "objective": "该步骤的明确实施要求与边界",\n'
        '      "acceptance": ["tests_pass"],\n'
        '      "required_capabilities": ["repo_read", "repo_write"],\n'
        '      "optional": false\n'
        '    }\n'
        '  ]\n'
        "}\n"
        "<!-- ROVE_PLAN_END -->\n"
    )


def build_step_prompt(
    run: Dict[str, Any],
    step: Dict[str, Any],
    project: Dict[str, Any],
    prior_artifacts: List[Dict[str, Any]],
) -> str:
    role = step['role']
    if role == 'reviewer':
        cwd = run.get('workspace_path') or project.get('path') or ''
        diff_section = review._collect_git_diff_summary(cwd) if cwd else ''
        art_lines = []
        for art in prior_artifacts[-6:]:
            art_lines.append(
                f"- `{art['kind']}` ({art.get('location') or 'workspace'}) · hash=`{art.get('content_hash')}`: {art.get('summary', '')[:240]}"
            )
        lines = [
            '# [独立只读交叉审查 / Orchestrator Cross-Review]',
            f'- **项目**: `{project.get("name", "")}` (`{cwd}`)',
            '- **权限**: 严格只读（严禁修改任何文件；请直接核验代码与 Git Diff，不要轻信前序开发自述）',
            '',
            '## 1. 用户原始目标',
            run['prompt'][:1600],
        ]
        if art_lines:
            lines.extend(['', '## 2. 前序阶段产物与指纹', '\n'.join(art_lines)])
        if diff_section:
            lines.extend(['', '## 3. 工作区真实 Git Diff 证据', diff_section])
        lines.extend([
            '',
            '## 4. 审查输出要求',
            '- 按严重度列出真实缺陷：`[P0]`（崩溃/安全/数据损坏）、`[P1]`（逻辑错误/边界遗漏）、`[P2]`（可维护性）。',
            '- 每条必须给出 `文件路径:行号 — 问题与证据`。',
            '- 回答最后一行**必须**输出：`FINDINGS_COUNT: <整数>`。',
        ])
        return '\n'.join(lines)

    try:
        scope = json.loads(step.get('files_scope_json') or '[]')
    except Exception:
        scope = []
    try:
        acceptance = json.loads(step.get('acceptance_json') or '[]')
    except Exception:
        acceptance = []

    lines = [
        f"# [协同执行步骤 · {step['title']}]",
        f"- **角色**: `{role}` | **项目**: `{project.get('name', '')}`",
        f"- **基线指纹**: `{run.get('baseline_ref') or 'workspace'}`",
        '',
        '## 用户总目标',
        run['prompt'][:1200],
        '',
        '## 本步骤具体任务',
        step['prompt'],
    ]
    if scope:
        lines.extend(['', '## 允许修改范围 (files_scope)', '\n'.join(f'- `{p}`' for p in scope)])
    if acceptance:
        lines.extend(['', '## 验收要求', '\n'.join(f'- {a}' for a in acceptance)])
    if prior_artifacts:
        lines.append('')
        lines.append('## 依赖产物与前序进度')
        for art in prior_artifacts[-4:]:
            lines.append(f"- [{art['kind']}] hash=`{art.get('content_hash')}`: {art.get('summary', '')[:260]}")
    return '\n'.join(lines)


def spawn_dispatch_worker(dispatch_info: Dict[str, Any]) -> None:
    """Spawn a detached workstation.worker process for a committed dispatch record."""
    job_id = dispatch_info['job_id']
    subprocess.Popen(
        [sys.executable, '-m', 'workstation.worker', job_id],
        cwd=db.ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
    ostore.mark_dispatch_spawned(dispatch_info['attempt_id'], dispatch_info['generation'])


def _route_step(
    run: Dict[str, Any],
    step: Dict[str, Any],
    *,
    exclude_engine: Optional[str] = None,
    exclude_model_family: Optional[str] = None,
) -> Dict[str, Any]:
    """Select the optimal (engine, model, effort, mode) for a given orchestration step."""
    role = step['role']
    try:
        policy = json.loads(run.get('policy_json') or '{}')
    except Exception:
        policy = {}
    base_mode = policy.get('mode') or 'standard'
    step_mode = 'plan' if role in ('planner', 'reviewer') else base_mode

    if step.get('preferred_engine'):
        eng = step['preferred_engine']
        caps = agents.get_engine_capabilities(eng)
        return {
            'engine': eng,
            'model': step.get('preferred_model'),
            'effort': step.get('preferred_effort') or 'medium',
            'mode': step_mode,
            'capabilities': caps,
            'reason': f'用户指定 {eng} 执行角色[{role}]',
        }

    try:
        req_caps = json.loads(step.get('required_capabilities_json') or '[]')
    except Exception:
        req_caps = []
    q_snap = quotas.get_cached_quotas() if hasattr(quotas, 'get_cached_quotas') else None
    route = model_router.select_model_for_role(
        role=role,
        prompt=step.get('prompt') or run['prompt'],
        mode=step_mode,
        quotas_snapshot=q_snap,
        required_capabilities=req_caps,
        exclude_engine=exclude_engine,
        exclude_model_family=exclude_model_family,
        strict_heterogeneous=bool(policy.get('strict_heterogeneous', False)),
    )
    route['mode'] = step_mode
    return route


def _dispatch_step(
    run: Dict[str, Any],
    step: Dict[str, Any],
    project: Dict[str, Any],
    *,
    prompt_override: Optional[str] = None,
    exclude_engine: Optional[str] = None,
    exclude_model_family: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    route = _route_step(
        run,
        step,
        exclude_engine=exclude_engine,
        exclude_model_family=exclude_model_family,
    )
    if prompt_override is not None:
        prompt_text = prompt_override
    elif step['role'] == 'planner':
        budget = json.loads(run.get('budget_json') or '{}')
        prompt_text = build_planner_prompt(
            run['prompt'],
            project,
            strategy='relay' if run['strategy'] in ('relay', 'auto') else run['strategy'],
            max_impl_steps=int(budget.get('max_impl_steps', schema.MAX_IMPL_STEPS)),
        )
    else:
        prior_artifacts = db.query(
            'SELECT * FROM orchestration_artifacts WHERE run_id=? ORDER BY created ASC',
            (run['id'],),
        )
        prompt_text = build_step_prompt(run, step, project, prior_artifacts)

    input_digest = hashlib.sha256(prompt_text.encode('utf-8', errors='replace')).hexdigest()[:16]
    dispatch_info = ostore.claim_step_and_create_dispatch(
        run_id=run['id'],
        step_id=step['id'],
        engine=route['engine'],
        model=route.get('model'),
        effort=route.get('effort'),
        mode=route['mode'],
        prompt_text=prompt_text,
        route_reason=route.get('reason') or '',
        capability_snapshot=route.get('capabilities') or {},
        input_digest=input_digest,
    )
    if not dispatch_info:
        return None

    _emit_orch_event(
        run['root_session_id'],
        run['id'],
        'route_selected',
        {
            'step_id': step['id'],
            'logical_key': step['logical_key'],
            'role': step['role'],
            'title': step['title'],
            'engine': dispatch_info['engine'],
            'model': dispatch_info['model'],
            'effort': dispatch_info['effort'],
            'reason': route.get('reason') or '',
            'degraded_reason': route.get('degraded_reason') or '',
        },
    )
    spawn_dispatch_worker(dispatch_info)
    return dispatch_info


def start_orchestration(
    session: Dict[str, Any],
    project: Dict[str, Any],
    prompt: str,
    *,
    strategy: str = 'relay',
    mode: str = 'standard',
    request_id: Optional[str] = None,
    budget_overrides: Optional[Dict[str, Any]] = None,
    policy_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create and start a new Multi-Agent Orchestration Run idempotently."""
    clean_prompt = (prompt or '').strip()
    if not clean_prompt:
        raise ValueError('协同任务提示词不能为空')

    eff_strategy = schema.normalize_strategy(strategy)
    if eff_strategy == 'off':
        eff_strategy = 'relay'
    # P0-P3 serial-first: parallel falls back to controlled relay unless explicitly enabled
    if eff_strategy == 'auto':
        eff_strategy = 'relay'

    req_id = (request_id or '').strip() or ('req_' + secrets.token_hex(8))
    cwd = db.session_workdir(session, project)
    git_head, ws_hash = review.compute_workspace_fingerprint(cwd)
    baseline_ref = f'{git_head}:{ws_hash}' if git_head else ws_hash

    policy = {
        'mode': mode or session.get('mode') or 'standard',
        'strict_heterogeneous': False,
        **(policy_overrides or {}),
    }
    budget = schema.default_budget(budget_overrides)

    run, created = ostore.create_run_idempotent(
        request_id=req_id,
        project_id=project['id'],
        root_session_id=session['id'],
        prompt=clean_prompt,
        strategy=eff_strategy,
        policy=policy,
        budget=budget,
        baseline_ref=baseline_ref,
        workspace_path=cwd,
        initial_status='planning',
    )
    if not created:
        return ostore.enrich_run_summary(run)

    # Emit visible user message + run_created event on root session
    db.event(
        session['id'],
        None,
        'user',
        {'text': clean_prompt, 'orchestration': eff_strategy, 'orch_run_id': run['id']},
    )
    _emit_orch_event(
        session['id'],
        run['id'],
        'run_created',
        {'strategy': eff_strategy, 'status': 'planning', 'baseline_ref': baseline_ref},
    )

    planner_step = db.one(
        "SELECT * FROM orchestration_steps WHERE run_id=? AND plan_version=? AND logical_key='planner'",
        (run['id'], run['plan_version']),
    )
    if planner_step:
        try:
            _dispatch_step(run, planner_step, project)
        except Exception as exc:
            ostore.transition_run_status(
                run['id'],
                'needs_attention',
                wait_reason=str(exc)[:300],
                clear_root_active=True,
            )
            _emit_orch_event(session['id'], run['id'], 'run_finished', {'status': 'needs_attention', 'reason': str(exc)[:300]})

    return ostore.enrich_run_summary(ostore.get_run(run['id']))


def _last_coder_info(run_id: str) -> tuple[Optional[str], Optional[str]]:
    row = db.one(
        "SELECT a.engine, a.model FROM orchestration_attempts a "
        "JOIN orchestration_steps s ON s.id=a.step_id "
        "WHERE a.run_id=? AND s.role LIKE 'coder_%' AND a.outcome='completed' "
        "ORDER BY a.ended DESC LIMIT 1",
        (run_id,),
    )
    if not row:
        return None, None
    eng = row['engine']
    fam = model_router.infer_model_family(eng, row.get('model'))
    return eng, fam


def on_worker_finished(
    job: Dict[str, Any],
    session: Dict[str, Any],
    project: Dict[str, Any],
    outcome: Dict[str, Any],
) -> None:
    """Deterministic state-machine advancement when a sub-session Worker finishes."""
    run_id = job.get('orch_run_id')
    step_id = job.get('orch_step_id')
    attempt_id = job.get('orch_attempt_id')
    generation = job.get('orch_generation')
    if not (run_id and step_id and attempt_id and generation is not None):
        return

    reply_text = _extract_job_reply(session['id'], job['id'])
    out_digest = hashlib.sha256(reply_text.encode('utf-8', errors='replace')).hexdigest()[:16] if reply_text else ''
    status_str = outcome.get('status') or 'failed'
    err_msg = outcome.get('error') or ''

    att = ostore.record_attempt_completion_cas(
        attempt_id=attempt_id,
        generation=int(generation),
        outcome=status_str,
        output_digest=out_digest,
        error_class=status_str if status_str != 'completed' else '',
        error_message=err_msg,
    )
    if att is None:
        # Duplicate callback or stale generation: ignore safely
        return

    run = ostore.get_run(run_id)
    step = db.one('SELECT * FROM orchestration_steps WHERE id=?', (step_id,))
    if not run or not step:
        return

    root_id = run['root_session_id']
    _emit_orch_event(
        root_id,
        run_id,
        'step_changed',
        {'step_id': step_id, 'logical_key': step['logical_key'], 'role': step['role'], 'status': step['status']},
    )

    if run['status'] in schema.RUN_TERMINAL_STATES or run['status'] == 'cancelling':
        ostore.transition_run_status(run_id, 'cancelled', terminal_reason=run.get('terminal_reason') or '已取消', clear_root_active=True)
        _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'cancelled'})
        return

    if run['status'] == 'pausing':
        ostore.transition_run_status(run_id, 'paused', wait_reason=run.get('wait_reason') or '用户已暂停')
        _emit_orch_event(root_id, run_id, 'step_changed', {'run_status': 'paused'})
        return

    if status_str == 'stopped':
        ostore.transition_run_status(run_id, 'cancelled', terminal_reason='子任务被中断停止', clear_root_active=True)
        _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'cancelled'})
        return

    if status_str != 'completed':
        if step.get('optional'):
            db.execute("UPDATE orchestration_steps SET status='skipped', updated=? WHERE id=?", (time.time(), step_id))
        else:
            ostore.transition_run_status(
                run_id,
                'needs_attention',
                wait_reason=f"步骤 [{step['title']}] 执行失败: {err_msg[:240]}",
                clear_root_active=True,
            )
            _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'needs_attention', 'reason': err_msg[:240]})
            return

    cwd = run.get('workspace_path') or project.get('path') or ''
    git_head, ws_hash = review.compute_workspace_fingerprint(cwd)
    budget = json.loads(run.get('budget_json') or '{}')

    # Stage 1: Planner finished -> validate & materialize plan steps
    if step['role'] == 'planner':
        parsed_plan, parse_err = schema.extract_plan_block(reply_text)
        validated_plan = None
        if parsed_plan is not None and not parse_err:
            try:
                validated_plan = schema.validate_orchestration_plan(
                    parsed_plan,
                    cwd,
                    max_impl_steps=int(budget.get('max_impl_steps', schema.MAX_IMPL_STEPS)),
                )
            except schema.PlanValidationError as exc:
                parse_err = str(exc)

        if validated_plan is None:
            max_plan_retries = int(budget.get('max_plan_retries', 1))
            if int(run.get('plan_retries') or 0) < max_plan_retries:
                ostore.transition_run_status(run_id, 'planning', increment_plan_retries=True)
                correction_prompt = (
                    f"上一次计划输出未通过协议校验：{parse_err}\n\n"
                    "请仅修正 JSON 格式并重新输出唯一一个 <!-- ROVE_PLAN_START --> ... <!-- ROVE_PLAN_END --> 计划块。"
                )
                db.execute("UPDATE orchestration_steps SET status='ready', updated=? WHERE id=?", (time.time(), step_id))
                _dispatch_step(ostore.get_run(run_id), db.one('SELECT * FROM orchestration_steps WHERE id=?', (step_id,)), project, prompt_override=correction_prompt)
                return
            ostore.transition_run_status(
                run_id,
                'needs_attention',
                wait_reason=f'Planner 计划协议校验失败: {parse_err}',
                clear_root_active=True,
            )
            _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'needs_attention', 'reason': parse_err})
            return

        ostore.record_artifact(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            kind='plan',
            location='ROVE_PLAN',
            content_hash=out_digest,
            baseline_ref=run.get('baseline_ref') or '',
            summary=f"已拆解 {len(validated_plan['steps'])} 个步骤",
            metadata={'steps': [s['id'] for s in validated_plan['steps']]},
        )
        ostore.insert_plan_steps(run_id, run['plan_version'], validated_plan['steps'], start_index=1)
        run = ostore.transition_run_status(run_id, 'running')
        _dispatch_next_ready_or_review(run, project, cwd, ws_hash)
        return

    # Stage 2/4: Implementation or Fix step finished -> record workspace artifact & advance
    if step['role'].startswith('coder_') or step['role'] == 'verifier':
        ostore.record_artifact(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            kind='workspace_diff',
            location=cwd,
            content_hash=ws_hash,
            baseline_ref=git_head,
            summary=reply_text[:400] or f"步骤 {step['title']} 已执行",
        )
        _emit_orch_event(root_id, run_id, 'artifact_ready', {'step_id': step_id, 'workspace_hash': ws_hash})
        ostore.refresh_ready_steps(run_id, run['plan_version'])
        _dispatch_next_ready_or_review(run, project, cwd, ws_hash)
        return

    # Stage 3: Reviewer finished -> check findings & freshness
    if step['role'] == 'reviewer':
        parsed_rev = schema.parse_review_result(reply_text)
        ostore.record_artifact(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            kind='review_report',
            location=cwd,
            content_hash=ws_hash,
            baseline_ref=git_head,
            summary=f"FINDINGS_COUNT={parsed_rev['findings_count']}",
            metadata=parsed_rev,
        )
        if not parsed_rev['valid_count']:
            ostore.transition_run_status(
                run_id,
                'needs_attention',
                wait_reason='独立审查未输出有效的 FINDINGS_COUNT 计数，已阻塞自动通过',
                clear_root_active=True,
            )
            _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'needs_attention'})
            return

        if parsed_rev['findings_count'] == 0 and parsed_rev['blocking_count'] == 0:
            ostore.transition_run_status(
                run_id,
                'completed',
                terminal_reason='所有阶段执行完成且独立交叉审查通过 (FINDINGS_COUNT: 0)',
                clear_root_active=True,
            )
            _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'completed', 'findings_count': 0})
            return

        max_fix = int(budget.get('max_fix_rounds', schema.DEFAULT_MAX_FIX_ROUNDS))
        if int(run.get('fix_rounds') or 0) < max_fix:
            next_round = int(run.get('fix_rounds') or 0) + 1
            run = ostore.transition_run_status(run_id, 'running', increment_fix_rounds=True)
            fix_step = ostore.insert_dynamic_step(
                run_id=run_id,
                plan_version=run['plan_version'],
                logical_key=f'auto_fix_{next_round}',
                title=f'自动返修（第 {next_round}/{max_fix} 轮）',
                role='coder_backend',
                prompt=(
                    f"独立交叉审查发现了 {parsed_rev['findings_count']} 个问题，请逐条核实并修复：\n\n"
                    f"{reply_text[:2400]}"
                ),
                required_capabilities=['repo_read', 'repo_write'],
            )
            _dispatch_step(run, fix_step, project)
            return

        ostore.transition_run_status(
            run_id,
            'needs_attention',
            wait_reason=f"已达到最大返修轮次 ({max_fix})，仍存在 {parsed_rev['findings_count']} 条审查发现",
            clear_root_active=True,
        )
        _emit_orch_event(root_id, run_id, 'run_finished', {'status': 'needs_attention', 'findings_count': parsed_rev['findings_count']})


def _dispatch_next_ready_or_review(
    run: Dict[str, Any],
    project: Dict[str, Any],
    cwd: str,
    ws_hash: str,
) -> None:
    run_id = run['id']
    plan_ver = run['plan_version']
    ready = db.one(
        "SELECT * FROM orchestration_steps WHERE run_id=? AND plan_version=? AND status='ready' ORDER BY step_index ASC LIMIT 1",
        (run_id, plan_ver),
    )
    if ready:
        try:
            _dispatch_step(run, ready, project)
        except Exception as exc:
            ostore.transition_run_status(run_id, 'needs_attention', wait_reason=str(exc)[:300], clear_root_active=True)
            _emit_orch_event(run['root_session_id'], run_id, 'run_finished', {'status': 'needs_attention', 'reason': str(exc)[:300]})
        return

    # Check if any steps are still pending/dispatched/running
    active = db.one(
        "SELECT id FROM orchestration_steps WHERE run_id=? AND plan_version=? AND status IN ('pending','dispatched','running') LIMIT 1",
        (run_id, plan_ver),
    )
    if active:
        return

    # All implementation steps finished -> trigger Cross-Reviewer if not yet reviewed at current ws_hash
    run = ostore.transition_run_status(run_id, 'verifying')
    last_eng, last_fam = _last_coder_info(run_id)
    rev_count = db.one(
        "SELECT count(*) AS c FROM orchestration_steps WHERE run_id=? AND plan_version=? AND role='reviewer'",
        (run_id, plan_ver),
    )['c']
    rev_step = ostore.insert_dynamic_step(
        run_id=run_id,
        plan_version=plan_ver,
        logical_key=f'reviewer_{rev_count + 1}',
        title=f'独立交叉审查 #{rev_count + 1}',
        role='reviewer',
        prompt='对当前工作区修改进行独立只读复核',
        required_capabilities=['repo_read', 'read_only_enforced'],
    )
    try:
        _dispatch_step(run, rev_step, project, exclude_engine=last_eng, exclude_model_family=last_fam)
    except Exception as exc:
        ostore.transition_run_status(run_id, 'needs_attention', wait_reason=str(exc)[:300], clear_root_active=True)
        _emit_orch_event(run['root_session_id'], run_id, 'run_finished', {'status': 'needs_attention', 'reason': str(exc)[:300]})


def recover_pending_dispatches() -> int:
    """Startup/periodic recovery: spawn workers for dispatches committed before a crash/restart."""
    rows = db.query(
        "SELECT d.*, j.status AS job_status, r.status AS run_status "
        "FROM orchestration_dispatches d "
        "JOIN jobs j ON j.id = d.job_id "
        "JOIN orchestration_runs r ON r.id = d.run_id "
        "WHERE d.status = 'pending' AND j.status = 'queued' "
        "AND r.status NOT IN ('cancelled','failed','completed','paused','pausing','cancelling','needs_attention')"
    )
    count = 0
    for row in rows:
        spawn_dispatch_worker({
            'attempt_id': row['attempt_id'],
            'job_id': row['job_id'],
            'generation': row['generation'],
        })
        count += 1
    return count


def handle_run_action(
    run_id: str,
    action: str,
    expected_revision: int,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle user control actions (pause/resume/cancel/skip_optional/change_model) with CAS revision check."""
    run = ostore.get_run(run_id)
    if not run:
        raise ValueError('未找到协同编排任务')
    if int(run['revision']) != int(expected_revision):
        raise RuntimeError('状态已被其他操作更新，请刷新后重试 (revision conflict)')

    act = (action or '').strip().lower()
    if act == 'cancel':
        updated = ostore.cancel_run_atomic(run_id, reason='用户停止协同任务')
        _emit_orch_event(run['root_session_id'], run_id, 'run_finished', {'status': 'cancelled'})
        return ostore.enrich_run_summary(updated)

    if act == 'pause':
        active_att = db.one(
            "SELECT id FROM orchestration_attempts WHERE run_id=? AND outcome IS NULL LIMIT 1",
            (run_id,),
        )
        next_st = 'pausing' if active_att else 'paused'
        updated = ostore.transition_run_status(run_id, next_st, wait_reason='用户暂停协同编排')
        _emit_orch_event(run['root_session_id'], run_id, 'step_changed', {'run_status': next_st})
        return ostore.enrich_run_summary(updated)

    if act == 'resume':
        if run['status'] not in ('paused', 'needs_attention'):
            raise ValueError(f'当前状态 {run["status"]} 不支持恢复')
        proj = db.one('SELECT * FROM projects WHERE id=?', (run['project_id'],))
        updated = ostore.transition_run_status(run_id, 'running', wait_reason='')
        cwd = run.get('workspace_path') or (proj or {}).get('path') or ''
        _, ws_hash = review.compute_workspace_fingerprint(cwd)
        if proj:
            _dispatch_next_ready_or_review(updated, proj, cwd, ws_hash)
        return ostore.enrich_run_summary(ostore.get_run(run_id))

    if act == 'skip_optional':
        step_id = (payload or {}).get('step_id')
        step = db.one('SELECT * FROM orchestration_steps WHERE id=? AND run_id=?', (step_id, run_id))
        if not step or not step.get('optional'):
            raise ValueError('仅可选步骤支持跳过')
        if step['status'] not in ('pending', 'ready'):
            raise ValueError('仅未开始的可选步骤支持跳过')
        db.execute("UPDATE orchestration_steps SET status='skipped', updated=? WHERE id=?", (time.time(), step_id))
        ostore.refresh_ready_steps(run_id, run['plan_version'])
        updated = ostore.transition_run_status(run_id, run['status'])
        return ostore.enrich_run_summary(updated)

    if act == 'change_model':
        step_id = (payload or {}).get('step_id')
        engine = (payload or {}).get('engine')
        model = (payload or {}).get('model')
        effort = (payload or {}).get('effort')
        step = db.one('SELECT * FROM orchestration_steps WHERE id=? AND run_id=?', (step_id, run_id))
        if not step or step['status'] not in ('pending', 'ready'):
            raise ValueError('仅尚未启动的步骤支持更换模型')
        db.execute(
            'UPDATE orchestration_steps SET preferred_engine=?, preferred_model=?, preferred_effort=?, updated=? WHERE id=?',
            (engine, model, effort, time.time(), step_id),
        )
        updated = ostore.transition_run_status(run_id, run['status'])
        return ostore.enrich_run_summary(updated)

    raise ValueError(f'不支持的操作: {action}')
