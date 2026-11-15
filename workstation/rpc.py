import asyncio
import contextlib
import json
import os
import signal


class CodexRPC:
    """One task owns one official app-server. Close after each turn to release desktop locks."""
    def __init__(self, binary, notify, request):
        self.binary, self.notify, self.request = binary, notify, request
        self.pending, self.counter, self.handlers = {}, 0, set()

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, 'app-server', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=16 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        result = await self.call('initialize', {'clientInfo': {'name': 'walking_workstation', 'title': '行走工作台', 'version': '0.1.0'}})
        await self.send({'method': 'initialized', 'params': {}})
        return result

    async def send(self, msg):
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()

    async def call(self, method, params=None, timeout=60):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({'id': ident, 'method': method, 'params': params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def handle_request(self, msg):
        try:
            answer = await self.request(msg)
            await self.send({'id': msg['id'], **answer})
        except (ConnectionError, asyncio.CancelledError, BrokenPipeError):
            pass

    async def read(self):
        try:
            async for line in self.proc.stdout:
                msg = json.loads(line)
                if 'method' in msg:
                    if 'id' in msg:
                        task = asyncio.create_task(self.handle_request(msg))
                        self.handlers.add(task)
                        task.add_done_callback(self.handlers.discard)
                    else:
                        await self.notify(msg['method'], msg.get('params', {}))
                elif msg.get('id') in self.pending:
                    future = self.pending[msg['id']]
                    if not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(msg['error'].get('message', 'Codex RPC failed')))
                        else:
                            future.set_result(msg.get('result', {}))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('Codex app-server disconnected'))

    async def close(self):
        for task in list(self.handlers):
            task.cancel()
        if getattr(self, 'proc', None) and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGTERM)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 5)
                if self.proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    await self.proc.wait()
        if hasattr(self, 'reader'):
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
