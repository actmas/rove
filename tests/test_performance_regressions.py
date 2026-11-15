import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from workstation import store as db, cluster_sync


class PerformanceRegressions(unittest.TestCase):
    def setUp(self):
        self.original_data = db.DATA
        self.temp = tempfile.TemporaryDirectory()
        db.DATA = Path(self.temp.name) / 'data'
        db.init()

    def tearDown(self):
        connection = getattr(db._local, 'conn', None)
        if connection is not None:
            connection.close()
        db.DATA = self.original_data
        self.temp.cleanup()

    def test_login_preclaims_attempts_and_limits_concurrent_hashes(self):
        from fastapi import HTTPException, Response
        from workstation import server
        async def scenario():
            peer = 'login-regression-peer'
            active = 0
            peak = 0
            async def slow_hash(*args):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.01)
                active -= 1
                return 'incorrect'
            async def attempt():
                try:
                    await server.login(server.Login(password='wrong'), SimpleNamespace(client=SimpleNamespace(host=peer)), Response())
                except HTTPException as exc:
                    return exc.status_code
            server.attempts.pop(peer, None)
            try:
                with patch.object(server.asyncio, 'to_thread', side_effect=slow_hash):
                    first = await asyncio.gather(*(attempt() for _ in range(20)))
                    self.assertEqual(first.count(401), 4)
                    self.assertEqual(first.count(429), 16)
                    self.assertEqual(len(server.attempts[peer]), 4)
                    for _ in range(8):
                        self.assertEqual(await attempt(), 401)
                    self.assertEqual(await attempt(), 429)
                    self.assertEqual(len(server.attempts[peer]), 12)
                    self.assertEqual(peak, 4)
            finally:
                server.attempts.pop(peer, None)
        asyncio.run(scenario())

    def test_cancelled_login_holds_capacity_until_hash_finishes(self):
        from fastapi import Response
        from workstation import server
        async def scenario():
            started = asyncio.Event()
            finish = asyncio.Event()
            async def slow_hash(*args):
                started.set()
                await finish.wait()
                return 'incorrect'
            with patch.object(server.asyncio, 'to_thread', side_effect=slow_hash):
                task = asyncio.create_task(server.login(server.Login(password='wrong'), SimpleNamespace(client=SimpleNamespace(host='cancel-test')), Response()))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(server.password_checks, 1)
                finish.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(server.password_checks, 0)
            server.attempts.pop('cancel-test', None)
        asyncio.run(scenario())

    def test_http_import_reports_mirror_failure_without_blocking_loop(self):
        from workstation import server
        started = threading.Event()
        finish = threading.Event()
        class Request:
            headers = {'x-snapshot-filename': 'rove-snapshot-regression.tar.gz'}
            async def stream(self):
                yield b'x' * 128
        def mirror(path):
            started.set()
            finish.wait(2)
            raise OSError('disk full')
        async def scenario():
            with patch.object(cluster_sync, 'mirror_snapshot_to_secondary', side_effect=mirror), patch.object(cluster_sync, 'restore_snapshot', return_value={'ok': True}):
                task = asyncio.create_task(server.import_cluster_snapshot(Request()))
                try:
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(.005)
                    self.assertTrue(started.is_set())
                    # The event loop reaches this point while the mirror thread waits.
                    self.assertFalse(finish.is_set())
                finally:
                    finish.set()
                result = await task
                self.assertFalse(result['ok'])
                self.assertEqual(result['secondary_backup'], {'status': 'failed', 'error': 'disk full'})
        asyncio.run(scenario())

    def test_mirror_failure_preserves_existing_archive(self):
        source = Path(self.temp.name) / 'rove-snapshot-source.tar.gz'
        source.write_bytes(b'new')
        secondary = Path(self.temp.name) / 'secondary'
        secondary.mkdir()
        existing = secondary / source.name
        existing.write_bytes(b'old')
        cluster_sync.update_cluster_config({'pve_secondary_backup_dir': str(secondary)})
        with patch.object(cluster_sync.shutil, 'copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cluster_sync.mirror_snapshot_to_secondary(source)
        self.assertEqual(existing.read_bytes(), b'old')
        self.assertEqual(list(secondary.glob('*.partial-*')), [])

    def test_http_upload_streams_file_and_propagates_remote_failure(self):
        received = []
        payload = b'snapshot' * 4096
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                body = json.dumps({'ok': False, 'secondary_backup': {'status': 'failed', 'error': 'disk full'}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        source = Path(self.temp.name) / 'snapshot.tar.gz'
        source.write_bytes(payload)
        snapshot = {'filename': 'rove-snapshot-stream.tar.gz', 'path': str(source), 'size_formatted': '32 KB'}
        cluster_sync.update_cluster_config({'peer_url': f'http://127.0.0.1:{http.server_port}', 'pve_ssh_host': ''})
        try:
            with patch.object(cluster_sync, 'create_snapshot', return_value=snapshot), patch.object(Path, 'read_bytes', side_effect=AssertionError('Whole-file read')):
                result = cluster_sync.push_snapshot_to_peer(False)
            self.assertEqual(received, [payload])
            self.assertFalse(result['ok'])
            self.assertIn('disk full', result['detail'])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

    def test_sync_rejects_overlap_and_releases_lock_after_failure(self):
        started = threading.Event()
        finish = threading.Event()
        errors = []
        def blocked_sync(include_projects):
            started.set()
            finish.wait(2)
            raise RuntimeError('test failure')
        def run():
            try:
                cluster_sync.push_snapshot_to_peer(False)
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch.object(cluster_sync, '_push_snapshot_to_peer', side_effect=blocked_sync):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                with self.assertRaisesRegex(RuntimeError, '已有快照同步'):
                    cluster_sync.push_snapshot_to_peer(False)
            finally:
                finish.set()
                thread.join()
        with patch.object(cluster_sync, '_push_snapshot_to_peer', return_value={'ok': True}):
            self.assertEqual(cluster_sync.push_snapshot_to_peer(False), {'ok': True})
        self.assertEqual(errors, ['test failure'])
