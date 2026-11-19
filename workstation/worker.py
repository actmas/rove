"""Detached job workers: no HTTP request or browser owns the execution lifetime."""
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time

from . import store as db
from . import agents
from . import checkpoints
from . import cloud_execution
from . import engines
from . import codex_sync
from . import antigravity_sync
from . import antigravity_hub
from . import webpush
from . import memories
from . import design_guard
from . import account_pool
from . import context_bridge
from . import input_handoff
from . import loop_guard
from . import model_router
from . import orchestrator
from . import quota_wait
from . import quotas
from . import review
from .rpc import CodexRPC

DELTA_METHODS = (
    'item/agentMessage/delta',
    'item/commandExecution/outputDelta',
    'item/reasoning/summaryTextDelta',
    'item/reasoning/textDelta',
    'item/plan/delta',
)


def _session_images(session_id, raw_images):
    try:
        names = json.loads(raw_images or '[]')
    except ValueError:
        return []
    base = (db.DATA / 'uploads' / session_id).resolve()
    paths = []
    for name in names:
        if not isinstance(name, str) or '/' in name or '\\' in name or name.startswith('.'):
            continue
        candidate = (base / name).resolve()
        if candidate.is_relative_to(base) and candidate.is_file():
            paths.append(candidate)
    return paths


def _codex_policy(mode):
    if mode == 'plan':
        return 'on-request', 'user', 'read-only', {'type': 'readOnly'}
    if mode == 'full':
        return 'on-request', 'user', 'danger-full-access', {'type': 'dangerFullAccess'}
    if mode == 'auto':
        return 'on-request', 'auto_review', 'workspace-write', {'type': 'workspaceWrite', 'networkAccess': True}
    return 'on-request', 'user', 'workspace-write', {'type': 'workspaceWrite', 'networkAccess': True}


async def run_codex(job, session, project):
    completed = asyncio.Event()
    outcome = {'status': 'failed', 'error': 'Codex 未返回回合完成事件'}
    turn = None
    buffered = None
    last_flush = time.monotonic()
    guard = loop_guard.ToolLoopGuard()

    def flush():
        nonlocal buffered, last_flush
        if buffered:
            db.event(session['id'], job['id'], 'codex', buffered)
            buffered = None
        last_flush = time.monotonic()

    async def notify(method, params):
        nonlocal buffered
        if method in DELTA_METHODS:
            same_stream = (
                buffered
                and buffered['method'] == method
                and buffered['params'].get('itemId') == params.get('itemId')
                and buffered['params'].get('summaryIndex') == params.get('summaryIndex')
                and buffered['params'].get('contentIndex') == params.get('contentIndex')
            )
            if buffered and not same_stream:
                flush()
            if buffered:
                buffered['params']['delta'] += params.get('delta', '')
            else:
                buffered = {'method': method, 'params': dict(params)}
            if time.monotonic() - last_flush >= .1 or len(buffered['params'].get('delta', '')) >= 8000:
                flush()
            return
        flush()
        if method in ('item/started', 'item/completed') and isinstance(params.get('item'), dict) and params['item'].get('id'):
            item = params['item']
            item_type = item.get('type') or ''
            db.execute(
                'INSERT OR REPLACE INTO synced_codex_items(session_id,item_id,updated_ordinal) VALUES(?,?,?)',
                (session['id'], item['id'], 999999999),
            )
            if item_type in ('commandExecution', 'fileChange', 'mcpToolCall'):
                tool_input = item.get('command') or item.get('changes') or item.get('arguments') or item_type
                if method == 'item/started':
                    guard.on_tool_use(item['id'], item_type, tool_input)
                else:
                    tool_out = item.get('aggregatedOutput') or item.get('output') or item.get('status') or ''
                    verdict = guard.on_tool_result(item['id'], item_type, tool_input, tool_out)
                    if verdict.get('status') == 'stop':
                        db.event(session['id'], job['id'], 'notice', {
                            'text': f"🛑【死循环守卫】检测到连续重复工具调用 ({verdict.get('reason')})，已自动触发安全中断。"
                        })
                        db.execute('UPDATE jobs SET stop=1 WHERE id=?', (job['id'],))
                    elif verdict.get('status') == 'suspected':
                        db.event(session['id'], job['id'], 'notice', {
                            'text': f"⚠️【死循环预警】工具 `{verdict.get('tool')}` 疑似重复空转 ({verdict.get('reason')}, {verdict.get('count')}次)，若无进展请手动停止。"
                        })
                        guard.grant_grace(20)
        # Persist the supported protocol stream; skip turn/diff/updated to avoid duplicating large diffs per edit.
        if method != 'turn/diff/updated' and (
            method.startswith(('item/', 'turn/'))
            or method in ('error', 'thread/status/changed', 'thread/tokenUsage/updated', 'model/rerouted', 'warning', 'guardianWarning', 'configWarning')
        ):
            db.event(session['id'], job['id'], 'codex', {'method': method, 'params': params})
        if method == 'turn/completed':
            value = params['turn']
            outcome.update(status={'completed': 'completed', 'interrupted': 'stopped'}.get(value['status'], 'failed'),
                           error=(value.get('error') or {}).get('message'))
            completed.set()

    async def request(msg):
        method = msg.get('method')
        if method not in (
            'item/commandExecution/requestApproval',
            'item/fileChange/requestApproval',
            'item/tool/requestUserInput',
            'item/permissions/requestApproval',
            'mcpServer/elicitation/request',
            'execCommandApproval',
            'applyPatchApproval',
        ):
            db.event(session['id'], job['id'], 'notice', {'text': '此客户端尚不支持交互请求：' + str(method)})
            return {'error': {'code': -32601, 'message': 'Unsupported interactive request; use native client for this action'}}
        params = msg.get('params') or {}
        if method == 'mcpServer/elicitation/request':
            request_params = params.get('request') or params
            schema = request_params.get('requestedSchema') or {}
            if schema.get('required'):
                return {'error': {'code': -32602, 'message': '此授权请求需要填写字段，请在原生客户端完成'}}
            params = {**params, 'reason': request_params.get('message') or '允许工具访问浏览器或电脑？'}
        ident = os.urandom(12).hex()
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,NULL,?)',
                   (ident, job['id'], method, json.dumps(params, ensure_ascii=False), time.time()))
        db.execute("UPDATE jobs SET status='waiting' WHERE id=?", (job['id'],))
        db.event(session['id'], job['id'], 'approval', {'id': ident, 'method': method, 'params': msg.get('params') or {}})
        await webpush.notify_job_approval(session, project, ident, method, msg.get('params') or {})
        guard.set_waiting_for_human(True)
        try:
            while not completed.is_set():
                approval = db.one('SELECT decision FROM approvals WHERE id=?', (ident,))
                state = db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))
                if approval['decision'] or state['stop']:
                    # Several parallel tools may be waiting; keep the remaining requests visible.
                    pending = db.one('SELECT count(*) AS n FROM approvals WHERE job_id=? AND id!=? AND decision IS NULL', (job['id'], ident))['n']
                    db.execute('UPDATE jobs SET status=? WHERE id=?', ('waiting' if pending else 'running', job['id']))
                    decision = approval['decision']
                    if method == 'mcpServer/elicitation/request':
                        action = {'accept': 'accept', 'decline': 'decline'}.get(decision, 'cancel')
                        result = {'action': action}
                        if action == 'accept':
                            result['content'] = {}
                        return {'result': result}
                    if method == 'item/tool/requestUserInput':
                        parsed_dec = json.loads(decision) if decision else {'answers': {}}
                        if isinstance(parsed_dec, dict) and parsed_dec.get('__secret_handoff'):
                            secret_vals = input_handoff.pop_secret_values(parsed_dec['__secret_handoff'])
                            if secret_vals is None:
                                db.event(session['id'], job['id'], 'notice', {
                                    'text': '⚠️ 保密输入已过期或未被读取，请重新提交。'
                                })
                                return {'result': {'answers': {}}}
                            return {'result': {'answers': {k: [v] for k, v in secret_vals.items()}}}
                        return {'result': parsed_dec}
                    if method == 'item/permissions/requestApproval':
                        if decision == 'accept':
                            return {'result': {'permissions': (msg.get('params') or {}).get('permissions', {}), 'scope': 'session'}}
                        return {'result': {'permissions': {}, 'scope': 'turn'}}
                    if method in ('execCommandApproval', 'applyPatchApproval'):
                        if decision == 'accept':
                            return {'result': {'decision': 'approved'}}
                        return {'result': {'decision': {'denied': {'rejection': 'User declined'}}}}
                    return {'result': {'decision': decision or 'cancel'}}
                await asyncio.sleep(.3)
        finally:
            guard.set_waiting_for_human(False)
        return {'result': {'decision': 'cancel'}}

    mode = job.get('mode') or session.get('mode') or 'standard'
    model, effort = engines.resolve_model(
        'codex', job.get('model') or session.get('model') or None, job.get('effort') or session.get('effort') or None)
    fast = bool(job.get('fast') if job.get('fast') is not None else session.get('fast'))
    approval_policy, approvals_reviewer, thread_sandbox, turn_sandbox = _codex_policy(mode)
    workdir = db.session_workdir(session, project)

    native_codex_id = db.get_codex_native_id(session)
    external_locked = bool(native_codex_id and codex_sync.is_thread_locked(native_codex_id))
    if external_locked and codex_sync.is_thread_running_externally(native_codex_id, session.get('rollout_path')):
        for _ in range(6):
            await asyncio.sleep(.5)
            external_locked = codex_sync.is_thread_locked(native_codex_id)
            if not external_locked or not codex_sync.is_thread_running_externally(native_codex_id, session.get('rollout_path')):
                break
        else:
            raise RuntimeError('Mac 本机 Codex 正在执行此会话，请等待当前回合完成后继续。')

    if db.get_antigravity_native_id(session):
        with contextlib.suppress(Exception):
            await asyncio.to_thread(antigravity_sync.sync_antigravity_session_events, session['id'])

    handoff_ctx = context_bridge.build_handoff_context(
        session, project, target_engine='codex', exclude_job_id=job['id']
    )

    acct_env = account_pool.resolve_account_env_for_session(session, 'codex')
    rpc = CodexRPC(db.config()['codex_bin'], notify, request, env=acct_env) if acct_env else CodexRPC(db.config()['codex_bin'], notify, request)
    try:
        await rpc.start()
        params = {
            'cwd': workdir,
            'approvalPolicy': approval_policy,
            'approvalsReviewer': approvals_reviewer,
            'sandbox': thread_sandbox,
        }
        if model:
            params['model'] = model
        if fast:
            params['serviceTier'] = 'priority'
        original_codex_id = native_codex_id
        resumed = False
        need_fork = external_locked
        if native_codex_id and not need_fork:
            try:
                result = await asyncio.wait_for(
                    rpc.call('thread/resume', {**params, 'threadId': native_codex_id, 'excludeTurns': True}),
                    timeout=10.0,
                )
                resumed = True
            except TimeoutError:
                need_fork = True
            except RuntimeError as exc:
                err_lower = str(exc).lower()
                if 'active writer' in err_lower or 'writer lock' in err_lower:
                    need_fork = True
                elif 'no rollout found' in err_lower or 'not found' in err_lower or 'does not exist' in err_lower:
                    native_codex_id = None
                else:
                    raise
        if native_codex_id and not resumed and need_fork:
            try:
                # Native desktop clients hold thread-writer-locks while open; fork preserves full context without blocking.
                result = await asyncio.wait_for(
                    rpc.call('thread/fork', {**params, 'threadId': native_codex_id, 'excludeTurns': True}),
                    timeout=10.0,
                )
                resumed = True
            except RuntimeError as exc:
                err_lower = str(exc).lower()
                if 'no rollout found' in err_lower or 'not found' in err_lower or 'does not exist' in err_lower:
                    native_codex_id = None
                else:
                    raise
        if not resumed:
            result = await rpc.call('thread/start', params)
        thread = result['thread']['id']
        new_rollout_path = result['thread'].get('path')
        if original_codex_id and thread != original_codex_id:
            db.execute(
                'UPDATE sessions SET native_id=?, native_codex_id=?, last_engine=?, cwd=COALESCE(cwd, ?), '
                'rollout_path=COALESCE(?, rollout_path), codex_synced_ordinal=-1, codex_rollout_offset=0 WHERE id=?',
                (thread, thread, 'codex', workdir, new_rollout_path, session['id']),
            )
            db.execute(
                'INSERT OR REPLACE INTO dismissed_codex_threads(native_id, dismissed_at) VALUES(?, ?)',
                (original_codex_id, time.time()),
            )
        else:
            db.execute(
                'UPDATE sessions SET native_id=?, native_codex_id=?, last_engine=?, cwd=COALESCE(cwd, ?) WHERE id=?',
                (thread, thread, 'codex', workdir, session['id']),
            )
        db.event(session['id'], job['id'], 'native_session', {'id': thread})

        inputs = [{'type': 'localImage', 'path': str(p)} for p in _session_images(session['id'], job.get('images'))]
        mem_ctx = memories.build_memory_context(project['id'])
        design_ctx = design_guard.get_design_directive(prompt_text=job['prompt'])
        prompt_parts = []
        if handoff_ctx:
            prompt_parts.append(handoff_ctx)
        elif not native_codex_id:
            if mem_ctx:
                prompt_parts.append(mem_ctx)
            if design_ctx:
                prompt_parts.append(design_ctx)
        prompt_parts.append(job['prompt'])
        codex_prompt = '\n\n'.join(prompt_parts)
        inputs.append({'type': 'text', 'text': codex_prompt})
        turn_params = {
            'threadId': thread,
            'cwd': workdir,
            'input': inputs,
            'approvalPolicy': approval_policy,
            'approvalsReviewer': approvals_reviewer,
            'sandboxPolicy': turn_sandbox,
            'summary': 'auto',
        }
        if model:
            turn_params['model'] = model
        if effort:
            turn_params['effort'] = effort
        if fast:
            turn_params['serviceTierForTurn'] = 'priority'

        result = await rpc.call('turn/start', turn_params)
        turn = result['turn']['id']
        stop_time = None
        while not completed.is_set():
            if rpc.reader.done():
                raise ConnectionError('Codex 连接中断；原生会话已保留')
            if db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))['stop']:
                if stop_time is None:
                    stop_time = time.monotonic()
                    with contextlib.suppress(TimeoutError, ConnectionError, RuntimeError):
                        await rpc.call('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=2)
                elif time.monotonic() - stop_time > 15:
                    outcome.update(status='stopped', error='已请求中断并关闭执行连接，请检查已发生的修改')
                    break
            try:
                await asyncio.wait_for(completed.wait(), .4)
            except asyncio.TimeoutError:
                pass
        return outcome
    finally:
        # Release thread write ownership before accepting the next web/native turn.
        flush()
        await rpc.close()


async def run_antigravity(job, session, project):
    images = _session_images(session['id'], job.get('images'))
    if db.get_codex_native_id(session):
        with contextlib.suppress(Exception):
            await asyncio.to_thread(codex_sync.sync_session_events, session['id'])

    hub = await antigravity_hub.discover_hub(auto_launch=True)
    if hub is not None:
        outcome = await antigravity_hub.run_hub_turn(hub, job, session, project, images)
        if outcome['status'] == 'completed' and job.get('execution_target') != 'cloud':
            engines.record('antigravity', {'status': 'ready', 'code': None, 'detail': '已通过本机 Antigravity Hub 完成模型调用'})
            return outcome
        if outcome['status'] == 'stopped':
            return outcome
        capability = engines.record('antigravity', engines.classify_error(outcome.get('error')))
        return {'status': 'failed', 'error': capability['detail']}

    mode = job.get('mode') or session.get('mode') or 'standard'
    model, effort = engines.resolve_model(
        'antigravity', job.get('model') or session.get('model') or None, job.get('effort') or session.get('effort') or None)
    agy_mode = {'standard': 'accept-edits', 'accept-edits': 'accept-edits', 'plan': 'plan', 'auto': 'auto', 'full': 'auto'}.get(mode, 'accept-edits')

    prompt_text = job['prompt']
    handoff_ctx = context_bridge.build_handoff_context(
        session, project, target_engine='antigravity', exclude_job_id=job['id']
    )
    native_agy_id = db.get_antigravity_native_id(session)
    if handoff_ctx:
        prompt_text = f'{handoff_ctx}\n\n{prompt_text}'
    elif not native_agy_id:
        mem_ctx = memories.build_memory_context(project['id'])
        design_ctx = design_guard.get_design_directive(prompt_text=job['prompt'])
        prompt_parts = []
        if mem_ctx:
            prompt_parts.append(mem_ctx)
        if design_ctx:
            prompt_parts.append(design_ctx)
        prompt_parts.append(prompt_text)
        prompt_text = '\n\n'.join(prompt_parts)
    if images:
        refs = '\n'.join(f'- {p}' for p in images)
        prompt_text = f'{prompt_text}\n\n[附加本机图片文件]\n{refs}'

    command = [db.config()['antigravity_bin'], '-p', prompt_text, '--output-format', 'stream-json', '--print-timeout', '0', '--mode', agy_mode]
    if model:
        command += ['--model', model]
    if effort in ('low', 'medium', 'high', 'xhigh', 'max'):
        command += ['--effort', effort]
    if native_agy_id:
        command += ['--conversation', native_agy_id]

    workdir = db.session_workdir(session, project)
    proc = await asyncio.create_subprocess_exec(*command, cwd=workdir, env=engines.antigravity_cli_env(),
                                              stdin=asyncio.subprocess.DEVNULL,
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                              limit=16 * 1024 * 1024)
    result = None

    async def stderr():
        async for line in proc.stderr:
            db.event(session['id'], job['id'], 'notice', {'text': line.decode(errors='replace')[:8000]})

    async def stdout():
        nonlocal result
        async for line in proc.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                db.event(session['id'], job['id'], 'notice', {'text': line.decode(errors='replace')[:8000]})
                continue
            db.event(session['id'], job['id'], 'antigravity', payload)
            native = payload.get('conversation_id') or payload.get('init', {}).get('conversation_id')
            if native:
                db.execute('UPDATE sessions SET native_id=?, native_antigravity_id=?, last_engine=? WHERE id=?', (native, native, 'antigravity', session['id']))
            if payload.get('event') == 'result':
                result = payload.get('result', payload)

    streams = [asyncio.create_task(stderr()), asyncio.create_task(stdout())]
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
                await asyncio.wait_for(proc.wait(), .4)
            except asyncio.TimeoutError:
                pass
        await asyncio.gather(*streams)
        if stop_time:
            return {'status': 'stopped', 'error': None}
        success = proc.returncode == 0 and result and result.get('status') == 'SUCCESS'
        error = None if success else ((result or {}).get('error') or 'Antigravity 未成功完成，请检查设置中的连接状态。')
        capability = engines.record('antigravity', {'status': 'ready', 'code': None, 'detail': '真实模型调用成功'} if success else engines.classify_error(error))
        return {'status': 'completed' if success else 'failed',
                'error': None if success else capability['detail']}
    finally:
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 5)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await asyncio.gather(*streams, return_exceptions=True)


async def run_codex_stoppable(job, session, project):
    """Monitor stopping during startup as well as active turns."""
    task = asyncio.create_task(run_codex(job, session, project))
    stop_time = None
    try:
        while not task.done():
            state = db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))
            if state and state['stop']:
                if stop_time is None:
                    stop_time = time.monotonic()
                if time.monotonic() - stop_time >= 18:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    return {'status': 'stopped', 'error': '执行连接已关闭，请检查已发生的修改'}
            await asyncio.wait({task}, timeout=.3)
        return await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def run(ident):
    job = db.one('SELECT * FROM jobs WHERE id=?', (ident,))
    # Transactional claim makes duplicate worker launches harmless.
    with db.transaction() as conn:
        claim = conn.execute("UPDATE jobs SET status='running',pid=?,started=? WHERE id=? AND status='queued'", (os.getpid(), time.time(), ident))
        if not claim.rowcount:
            return
    session = db.one('SELECT * FROM sessions WHERE id=?', (job['session_id'],))
    project = db.one('SELECT * FROM projects WHERE id=?', (session['project_id'],))
    if job.get('execution_target') != 'cloud':
        with contextlib.suppress(Exception):
            workdir = db.session_workdir(session, project)
            ckpt_ref = checkpoints.capture_checkpoint(workdir, session['id'], ident)
            if ckpt_ref:
                db.execute('UPDATE jobs SET checkpoint_ref=? WHERE id=?', (ckpt_ref, ident))
                job['checkpoint_ref'] = ckpt_ref
    raw_engine = job.get('engine') or session.get('engine') or 'codex'
    raw_model = job.get('model') or session.get('model')
    raw_effort = job.get('effort') or session.get('effort')
    raw_mode = job.get('mode') or session.get('mode') or 'standard'
    raw_fast = bool(job.get('fast') if job.get('fast') is not None else session.get('fast'))
    if job.get('execution_target') != 'cloud' and (
        raw_engine == 'auto' or raw_model == 'auto' or raw_effort == 'auto'
    ):
        q_snap = quotas.get_cached_quotas() if hasattr(quotas, 'get_cached_quotas') else None
        cand_engines = None
        if raw_engine == 'auto':
            cand = [e for e in ('codex', 'antigravity') if agents.is_agent_installed(e)]
            if cand:
                cand_engines = cand
        route_res = model_router.auto_select_model_and_effort(
            prompt=job.get('prompt') or '',
            mode=raw_mode,
            has_images=bool(_session_images(session['id'], job.get('images'))),
            requested_engine=raw_engine,
            requested_model=raw_model,
            requested_effort=raw_effort,
            requested_fast=raw_fast,
            quotas_snapshot=q_snap,
            installed_engines=cand_engines,
            preferred_engine=session.get('last_engine') or (session.get('engine') if session.get('engine') != 'auto' else None),
        )
        active_engine = route_res['engine']
        resolved_model, resolved_effort = engines.resolve_model(
            active_engine, route_res.get('model'), route_res.get('effort'), allow_auto=False
        )
        resolved_fast = int(bool(route_res.get('fast')))
        job = dict(job)
        job['engine'] = active_engine
        job['model'] = resolved_model
        job['effort'] = resolved_effort
        job['fast'] = resolved_fast
        db.execute(
            'UPDATE jobs SET engine=?, model=?, effort=?, fast=? WHERE id=?',
            (active_engine, resolved_model, resolved_effort, resolved_fast, ident),
        )
    else:
        active_engine = raw_engine
    db.event(session['id'], ident, 'job_status', {
        'status': 'running',
        'engine': active_engine,
        'model': None if job.get('execution_target') == 'cloud' else engines.resolve_model(active_engine, job.get('model') or session.get('model'), None)[0],
        'effort': job.get('effort') or session.get('effort'),
        'mode': raw_mode,
        'fast': bool(job.get('fast')),
    })
    try:
        if job['stop']:
            outcome = {'status': 'stopped', 'error': None}
        elif job.get('execution_target') == 'cloud':
            outcome = await cloud_execution.submit(job, session, project)
        elif active_engine == 'codex':
            outcome = await run_codex_stoppable(job, session, project)
        elif active_engine == 'antigravity':
            outcome = await run_antigravity(job, session, project)
        else:
            outcome = await agents.run_custom_agent(
                active_engine, job, session, project, _session_images(session['id'], job.get('images'))
            )
    except Exception as exc:
        outcome = {'status': 'failed', 'error': str(exc)[:4000]}
    after_ckpt = ''
    if job.get('execution_target') != 'cloud':
        with contextlib.suppress(Exception):
            workdir = db.session_workdir(session, project)
            after_ckpt = checkpoints.capture_checkpoint(workdir, session['id'], ident, phase='after')
    db.execute(
        'UPDATE jobs SET status=?,error=?,ended=?,checkpoint_after_ref=? WHERE id=?',
        (outcome['status'], outcome['error'], time.time(), after_ckpt, ident),
    )
    db.execute('UPDATE sessions SET updated=?, last_engine=? WHERE id=?', (time.time(), active_engine, session['id']))
    if outcome['status'] == 'stopped':
        with contextlib.suppress(Exception):
            fresh_s = db.one('SELECT * FROM sessions WHERE id=?', (session['id'],)) or session
            agy_nid = db.get_antigravity_native_id(fresh_s)
            if agy_nid:
                antigravity_sync.mark_external_stopped(session['id'], agy_nid)
            codex_nid = db.get_codex_native_id(fresh_s)
            if codex_nid:
                codex_sync.mark_external_stopped(session['id'], codex_nid, fresh_s.get('rollout_path'))
    db.event(session['id'], ident, 'job_status', outcome)
    if outcome['status'] == 'completed' and job.get('execution_target') != 'cloud':
        with contextlib.suppress(Exception):
            quota_wait.reset_attempts(session['id'])
        with contextlib.suppress(Exception):
            rev = db.one('SELECT * FROM reviews WHERE review_job_id=?', (ident,))
            if rev:
                turns = context_bridge.extract_session_turns(session['id'])
                rev_turn = next((t for t in reversed(turns) if t.get('job_id') == ident), None)
                rev_text = (rev_turn or {}).get('reply') or ''
                workdir = db.session_workdir(session, project)
                done_rev = review.complete_review_record(rev['id'], rev_text, workdir, failed=False)
                cnt = done_rev.get('findings_count') if done_rev else None
                cnt_label = f'{cnt} 条发现' if cnt is not None else '复核完成'
                preview = rev_text.strip()[:1200] or '（无详细输出）'
                db.event(rev['session_id'], ident, 'notice', {
                    'text': f"📋【独立 /review 结论（{done_rev.get('status')} · {cnt_label}）】\n{preview}"
                })
        with contextlib.suppress(Exception):
            memories.auto_record_from_job(session, job, project)
            if design_guard.is_ui_task(job.get('prompt', '')):
                workdir = db.session_workdir(session, project)
                audit_chunks = []
                diff_proc = subprocess.run(
                    ['git', '-C', workdir, 'diff', 'HEAD', '--unified=0'],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if diff_proc.returncode == 0 and diff_proc.stdout:
                    audit_chunks.append(diff_proc.stdout[:60000])
                for ev in db.query("SELECT payload FROM events WHERE job_id=? ORDER BY id DESC LIMIT 40", (ident,)):
                    with contextlib.suppress(Exception):
                        audit_chunks.append(ev.get('payload') or '')
                audit_text = '\n'.join(audit_chunks)
                findings = design_guard.audit_code_changes(audit_text)
                if findings:
                    db.event(session['id'], ident, 'notice', {
                        'text': f"🎨【Anti-Slop 审美审计】{findings[0]['advice']}"
                    })
    elif outcome['status'] == 'failed':
        err_msg = outcome.get('error') or ''
        with contextlib.suppress(Exception):
            rev = db.one('SELECT * FROM reviews WHERE review_job_id=?', (ident,))
            if rev:
                review.complete_review_record(rev['id'], err_msg, db.session_workdir(session, project), failed=True)
        if context_bridge.is_context_overflow_error(err_msg):
            with contextlib.suppress(Exception):
                side_effects = context_bridge.has_turn_side_effects(session['id'], ident)
                if active_engine == 'codex':
                    db.execute('UPDATE sessions SET native_codex_id=NULL, native_id=NULL WHERE id=?', (session['id'],))
                elif active_engine == 'antigravity':
                    db.execute('UPDATE sessions SET native_antigravity_id=NULL WHERE id=?', (session['id'],))
                else:
                    agents.set_session_native_agent_id(session['id'], active_engine, None)
                note = (
                    '🪟【上下文满窗已准备自动换窗】已归档旧原生窗口；因本轮已有部分副作用输出，请手动确认后发送下一条指令继续。'
                    if side_effects else
                    '🪟【上下文满窗已准备自动换窗】已归档旧原生窗口并准备好结构化交接上下文，可直接重试或发送下一条指令继续。'
                )
                db.event(session['id'], ident, 'notice', {'text': note})
        elif not job.get('orch_run_id') and quota_wait.is_quota_limit_error(err_msg, engine=active_engine):
            with contextlib.suppress(Exception):
                q_snap = quotas.get_cached_quotas() if hasattr(quotas, 'get_cached_quotas') else None
                has_out = context_bridge.has_turn_side_effects(session['id'], ident)
                wait_row = quota_wait.schedule_quota_wait(
                    session_id=session['id'],
                    job_id=ident,
                    engine=active_engine,
                    model=job.get('model') or session.get('model'),
                    prompt=job.get('prompt') or '',
                    has_output=has_out,
                    error_text=err_msg,
                    quotas_snapshot=q_snap,
                )
                if wait_row:
                    reset_str = quotas.format_local_time(wait_row['resume_at'])
                    db.event(session['id'], ident, 'notice', {
                        'text': f"⏳【额度耗尽自动续跑】将在 {reset_str} 自动继续本任务（第 {wait_row['attempts']}/{quota_wait.MAX_AUTO_CONTINUE_ATTEMPTS} 次）；发送新消息、切换模型或点击取消可随时终止等待。"
                    })
    if job.get('orch_run_id'):
        with contextlib.suppress(Exception):
            orchestrator.on_worker_finished(job, session, project, outcome)
        return
    await webpush.notify_job_outcome(ident, session, project, outcome)
    # Apply deferred runtime changes & dispatch next queued message if any
    with contextlib.suppress(Exception):
        fresh_sess = db.one('SELECT * FROM sessions WHERE id=?', (session['id'],))
        if fresh_sess and fresh_sess.get('pending_runtime_json') not in (None, '', '{}'):
            pending_rt = json.loads(fresh_sess['pending_runtime_json'])
            if isinstance(pending_rt, dict) and pending_rt:
                sets, args = [], []
                for col in ('engine', 'model', 'effort', 'mode', 'fast'):
                    if col in pending_rt and pending_rt[col] is not None:
                        sets.append(f'{col}=?')
                        args.append(pending_rt[col])
                sets.append("pending_runtime_json='{}'")
                args.append(session['id'])
                db.execute(f"UPDATE sessions SET {','.join(sets)} WHERE id=?", tuple(args))
                fresh_sess = db.one('SELECT * FROM sessions WHERE id=?', (session['id'],))
        if outcome['status'] in ('completed', 'stopped') and not bool((fresh_sess or session).get('queue_paused')):
            nxt = db.one(
                "SELECT * FROM session_queue WHERE session_id=? AND status='pending' "
                "ORDER BY steer DESC, created ASC LIMIT 1",
                (session['id'],),
            )
            if nxt:
                next_job_id = os.urandom(12).hex()
                now_ts = time.time()
                eff_sess = fresh_sess or session
                raw_q_engine = nxt.get('engine') or eff_sess.get('engine') or 'codex'
                raw_q_model = nxt.get('model') or eff_sess.get('model')
                raw_q_effort = nxt.get('effort') or eff_sess.get('effort')
                eff_mode = nxt.get('mode') or eff_sess.get('mode') or 'standard'
                raw_q_fast = bool(nxt['fast'] if nxt.get('fast') is not None else eff_sess.get('fast'))
                route_q = None
                if raw_q_engine == 'auto' or raw_q_model == 'auto' or raw_q_effort == 'auto':
                    q_snap = quotas.get_cached_quotas() if hasattr(quotas, 'get_cached_quotas') else None
                    cand_eng = [e for e in ('codex', 'antigravity') if agents.is_agent_installed(e)] if raw_q_engine == 'auto' else None
                    route_q = model_router.auto_select_model_and_effort(
                        prompt=nxt['prompt'],
                        mode=eff_mode,
                        has_images=bool(nxt.get('images') and nxt.get('images') != '[]'),
                        requested_engine=raw_q_engine,
                        requested_model=raw_q_model,
                        requested_effort=raw_q_effort,
                        requested_fast=raw_q_fast,
                        quotas_snapshot=q_snap,
                        installed_engines=cand_eng or None,
                        preferred_engine=eff_sess.get('last_engine') or (eff_sess.get('engine') if eff_sess.get('engine') != 'auto' else None),
                    )
                    eff_engine = route_q['engine']
                    eff_model, eff_effort = engines.resolve_model(eff_engine, route_q.get('model'), route_q.get('effort'), allow_auto=False)
                    eff_fast = int(bool(route_q.get('fast')))
                else:
                    eff_engine = raw_q_engine
                    eff_model, eff_effort = engines.resolve_model(eff_engine, raw_q_model, raw_q_effort, allow_auto=False)
                    eff_fast = int(raw_q_fast)
                with db.transaction() as conn:
                    claimed = conn.execute(
                        "UPDATE session_queue SET status='dispatched', updated=? WHERE id=? AND status='pending'",
                        (now_ts, nxt['id']),
                    )
                    if claimed.rowcount:
                        conn.execute(
                            'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,model,effort,mode,fast,images,engine) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (
                                next_job_id,
                                session['id'],
                                f"q-{nxt['id']}",
                                nxt['prompt'],
                                'queued',
                                now_ts,
                                eff_model,
                                eff_effort,
                                eff_mode,
                                eff_fast,
                                nxt.get('images') or '[]',
                                eff_engine,
                            ),
                        )
                        q_ev_payload = {
                            'text': nxt['prompt'],
                            'engine': eff_engine,
                            'model': eff_model,
                            'effort': eff_effort,
                            'mode': eff_mode,
                            'queued': True,
                        }
                        if route_q:
                            q_ev_payload['auto_routed'] = True
                            q_ev_payload['route_reason'] = route_q.get('reason') or ''
                        conn.execute(
                            'INSERT INTO events(session_id,job_id,kind,payload,created) VALUES(?,?,?,?,?)',
                            (
                                session['id'],
                                next_job_id,
                                'user',
                                json.dumps(q_ev_payload, ensure_ascii=False),
                                now_ts,
                            ),
                        )
                if claimed.rowcount:
                    subprocess.Popen(
                        [sys.executable, '-m', 'workstation.worker', next_job_id],
                        cwd=db.ROOT, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True, close_fds=True, env=os.environ.copy(),
                    )


if __name__ == '__main__':
    os.umask(0o077)
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]) if sys.platform == 'darwin' else None
    try:
        asyncio.run(run(sys.argv[1]))
    finally:
        if awake:
            awake.terminate()
            awake.wait()
