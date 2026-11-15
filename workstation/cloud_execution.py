"""Codex Cloud dispatch; remote tasks never reuse local thread IDs or apply changes."""
import asyncio
import contextlib
import json
import re

from . import store as db
from . import context_bridge


async def command(*args, cwd=None):
    binary = db.config().get('codex_bin')
    proc = await asyncio.create_subprocess_exec(binary, 'cloud', *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
        if proc.returncode:
            raise RuntimeError((stderr or stdout).decode(errors='replace')[-3000:])
        return stdout.decode(errors='replace').strip()
    finally:
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def submit(job, session, project):
    # Carry conversation context even if the previous turn used the same engine locally.
    turns = context_bridge.extract_session_turns(session['id'], exclude_job_id=job['id'])
    history = '\n\n'.join(f"用户：{t.get('prompt', '')}\n助手：{t.get('reply', '')}" for t in turns[-6:])
    prompt = f'已有对话背景（代码以云端环境仓库为准）：\n{history}\n\n当前任务：\n{job["prompt"]}'
    task = asyncio.create_task(command('exec', '--env', job['cloud_environment'], prompt,
        cwd=db.session_workdir(session, project)))
    try:
        while not task.done():
            if db.one('SELECT stop FROM jobs WHERE id=?', (job['id'],))['stop']:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                db.event(session['id'], job['id'], 'notice', {'text': '已停止本地提交等待；若云端已接收，请在 Codex 云端确认并取消任务。'})
                return {'status': 'stopped', 'error': None}
            await asyncio.wait({task}, timeout=0.4)
        output = await task
    except (TimeoutError, RuntimeError) as exc:
        raise RuntimeError(f'云端提交未确认，请先检查 Codex 云端任务列表再重试，避免重复提交：{exc}') from exc
    match = re.search(r'\btask_[A-Za-z0-9_-]+', output)
    if not match:
        raise RuntimeError('未识别到云端任务 ID，请先在 Codex 云端确认是否已提交，避免重复执行。')
    task_id = match.group()
    db.execute('UPDATE jobs SET cloud_task_id=?,model=NULL,effort=NULL,fast=0 WHERE id=?', (task_id, job['id']))
    db.event(session['id'], job['id'], 'cloud_task', {'task_id': task_id, 'job_id': job['id'], 'environment': job['cloud_environment']})
    db.event(session['id'], job['id'], 'antigravity', {'event': 'agy_step', 'step_id': f'cloud-{job["id"]}',
        'step_kind': 'agent', 'engine': 'codex', 'completed': True,
        'text': f'已提交到 Codex 云端：[{task_id}](https://chatgpt.com/codex/tasks/{task_id})。\n\n云端任务独立运行，模型与权限以云端环境为准。本地未执行、未上传未提交文件，也未自动应用云端修改。可在下方查询状态及差异。'})
    return {'status': 'submitted', 'error': None, 'cloud_submitted': True}


async def details(task_id):
    if not re.fullmatch(r'task_[A-Za-z0-9_-]+', task_id):
        raise ValueError('无效的云端任务 ID')
    status = await command('status', task_id)
    try:
        diff = await command('diff', task_id)
    except RuntimeError:
        diff = '云端尚未提供可读取的差异'
    return {'status': status, 'diff': diff}
