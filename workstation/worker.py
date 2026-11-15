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
from . import cloud_execution
from . import engines
from . import codex_sync
from . import antigravity_sync
from . import antigravity_hub
from . import webpush
from . import memories
from . import design_guard
from . import context_bridge
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
            db.execute(
                'INSERT OR REPLACE INTO synced_codex_items(session_id,item_id,updated_ordinal) VALUES(?,?,?)',
                (session['id'], params['item']['id'], 999999999),
            )
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
                    return {'result': json.loads(decision) if decision else {'answers': {}}}
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
        return {'result': {'decision': 'cancel'}}

    mode = job.get('mode') or session.get('mode') or 'standard'
    model, effort = engines.resolve_model(
        'codex', job.get('model') or session.get('model') or None, job.get('effort') or session.get('effort') or None)
    fast = bool(job.get('fast') if job.get('fast') is not None else session.get('fast'))
    approval_policy, approvals_reviewer, thread_sandbox, turn_sandbox = _codex_policy(mode)
    workdir = db.session_workdir(session, project)

    native_codex_id = db.get_codex_native_id(session)
    if native_codex_id and codex_sync.is_thread_locked(native_codex_id):
        for _ in range(6):
            await asyncio.sleep(.5)
            if not codex_sync.is_thread_locked(native_codex_id):
                break
        else:
            if codex_sync.is_thread_running_externally(native_codex_id, session.get('rollout_path')):
                raise RuntimeError('Mac 本机 Codex 正在执行此会话，请等待当前回合完成后继续。')
            raise RuntimeError('此原生会话正被其他客户端（如飞书桥接或外部进程）占用写入锁，请先释放占用端或新建会话。')

    if db.get_antigravity_native_id(session):
        with contextlib.suppress(Exception):
            await asyncio.to_thread(antigravity_sync.sync_antigravity_session_events, session['id'])

    handoff_ctx = context_bridge.build_handoff_context(
        session, project, target_engine='codex', exclude_job_id=job['id']
    )

    rpc = CodexRPC(db.config()['codex_bin'], notify, request)
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
        resumed = False
        if native_codex_id:
            try:
                result = await asyncio.wait_for(
                    rpc.call('thread/resume', {**params, 'threadId': native_codex_id, 'excludeTurns': True}),
                    timeout=10.0,
                )
                resumed = True
            except TimeoutError as exc:
                raise RuntimeError('恢复原生会话超时：该会话正被本机其他 Codex 客户端占用写入锁。') from exc
            except RuntimeError as exc:
                err_lower = str(exc).lower()
                if 'no rollout found' in err_lower or 'not found' in err_lower or 'does not exist' in err_lower:
                    native_codex_id = None
                else:
                    raise
        if not resumed:
            result = await rpc.call('thread/start', params)
        thread = result['thread']['id']
        db.execute('UPDATE sessions SET native_id=?, native_codex_id=?, last_engine=?, cwd=COALESCE(cwd, ?) WHERE id=?', (thread, thread, 'codex', workdir, session['id']))
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
    active_engine = job.get('engine') or session.get('engine') or 'codex'
    db.event(session['id'], ident, 'job_status', {
        'status': 'running',
        'engine': active_engine,
        'model': None if job.get('execution_target') == 'cloud' else engines.resolve_model(active_engine, job.get('model') or session.get('model'), None)[0],
        'effort': job.get('effort') or session.get('effort'),
        'mode': job.get('mode') or session.get('mode') or 'standard',
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
    db.execute('UPDATE jobs SET status=?,error=?,ended=? WHERE id=?', (outcome['status'], outcome['error'], time.time(), ident))
    db.execute('UPDATE sessions SET updated=?, last_engine=? WHERE id=?', (time.time(), active_engine, session['id']))
    db.event(session['id'], ident, 'job_status', outcome)
    if outcome['status'] == 'completed' and job.get('execution_target') != 'cloud':
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
    await webpush.notify_job_outcome(ident, session, project, outcome)


if __name__ == '__main__':
    os.umask(0o077)
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())]) if sys.platform == 'darwin' else None
    try:
        asyncio.run(run(sys.argv[1]))
    finally:
        if awake:
            awake.terminate()
            awake.wait()
