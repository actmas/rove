import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

# Import-time initialization is isolated too; tests never read production credentials.
_bootstrap = tempfile.TemporaryDirectory()
os.environ['WORKSTATION_DATA'] = _bootstrap.name
from fastapi.testclient import TestClient
from workstation import store as db
from workstation.server import app
from workstation import engines


class WorkstationTests(unittest.TestCase):
    def setUp(self):
        self._orig_data = db.DATA
        self.temp = tempfile.TemporaryDirectory()
        db.DATA = Path(self.temp.name) / 'data'
        db.init()
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        (self.project / 'main.py').write_text('print("hello")\n')
        (self.project / '.env').write_text('private-value')
        (self.project / 'outside').symlink_to('/etc/hosts')
        config = db.config()
        config['allowed_roots'] = [str(self.project)]
        db.write_private(db.DATA / 'config.json', json.dumps(config))
        db.execute('DELETE FROM projects')
        db.execute('INSERT INTO projects VALUES(?,?,?)', ('project', 'test', str(self.project)))
        self.client = TestClient(app, base_url='http://localhost:8765')
        self.client.__enter__()
        self.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        password = (db.DATA / 'access-password.txt').read_text().strip()
        response = self.client.post('/api/login', json={'password': password}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.spawn = patch('workstation.server.spawn', return_value=777)
        self.spawn.start()

    def tearDown(self):
        self.spawn.stop()
        db.close_all_connections()
        self.client.__exit__(None, None, None)
        db.DATA = self._orig_data
        self.temp.cleanup()

    def post(self, path, body=None):
        return self.client.post('/api/' + path, json=body or {}, headers=self.headers)

    def session(self):
        response = self.post('sessions', {'project_id': 'project', 'engine': 'codex'})
        self.assertEqual(response.status_code, 200)
        return response.json()['id']

    def test_rendered_images_resolve_against_session_project(self):
        sid = self.session()
        image = self.project / '测试 图片.svg'
        image.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        response = self.client.get('/api/media', params={'path': image.name, 'session_id': sid})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('image/svg+xml'))
        self.assertEqual(response.text, image.read_text())
        self.assertEqual(self.client.get('/api/media', params={'path': '.env', 'session_id': sid}).status_code, 400)
        outside = Path(self.temp.name) / 'outside.png'
        outside.write_bytes(b'image')
        (self.project / 'escape.png').symlink_to(outside)
        self.assertEqual(self.client.get('/api/media', params={'path': 'escape.png', 'session_id': sid}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/media', params={'path': str(image)}).status_code, 401)

    def test_task_generated_files_view_and_download(self):
        sid = self.session()
        report = self.project / '输出报告.md'
        report.write_text('# 任务结果\n\n完成。\n', encoding='utf-8')
        archive = self.project / 'bundle.zip'
        archive.write_bytes(b'PK\x03\x04\x00\x00binary')
        html_file = self.project / 'preview.html'
        html_file.write_text('<h1>Hi</h1><script>alert(1)</script>', encoding='utf-8')
        (self.project / '.env').write_text('SECRET=123', encoding='utf-8')

        # 1. Meta & inline view via relative path, file://, sandbox:, and :line suffix
        meta = self.client.get('/api/files/meta', params={'path': '输出报告.md:12', 'session_id': sid})
        self.assertEqual(meta.status_code, 200)
        payload = meta.json()
        self.assertEqual(payload['name'], '输出报告.md')
        self.assertEqual(payload['kind'], 'markdown')
        self.assertEqual(payload['relative_path'], '输出报告.md')
        self.assertIn('任务结果', payload['preview_text'])

        raw_file_url = self.client.get('/api/files', params={'path': f'file://{report}', 'session_id': sid})
        self.assertEqual(raw_file_url.status_code, 200)
        self.assertIn('任务结果', raw_file_url.text)

        sandbox_dl = self.client.get(
            '/api/files',
            params={'path': f'sandbox:{archive}', 'session_id': sid, 'download': '1'},
        )
        self.assertEqual(sandbox_dl.status_code, 200)
        self.assertEqual(sandbox_dl.content, b'PK\x03\x04\x00\x00binary')
        self.assertIn('attachment;', sandbox_dl.headers.get('content-disposition', ''))

        # 2. Unicode download filename header
        cn_dl = self.client.get('/api/files', params={'path': '输出报告.md', 'session_id': sid, 'download': '1'})
        self.assertEqual(cn_dl.status_code, 200)
        self.assertIn("filename*=UTF-8''", cn_dl.headers.get('content-disposition', ''))

        # 3. HTML inline view enforces sandbox CSP
        html_resp = self.client.get('/api/files', params={'path': 'preview.html', 'session_id': sid})
        self.assertEqual(html_resp.status_code, 200)
        self.assertIn('sandbox', html_resp.headers.get('content-security-policy', ''))

        # 4. Security: sensitive dotfiles and symlink escapes are blocked; missing returns 404
        self.assertEqual(self.client.get('/api/files', params={'path': '.env', 'session_id': sid}).status_code, 403)
        outside = Path(self.temp.name) / 'secret.txt'
        outside.write_text('nope', encoding='utf-8')
        (self.project / 'escape.txt').symlink_to(outside)
        self.assertEqual(self.client.get('/api/files', params={'path': 'escape.txt', 'session_id': sid}).status_code, 403)
        self.assertEqual(self.client.get('/api/files', params={'path': 'missing.txt', 'session_id': sid}).status_code, 404)

        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/files', params={'path': str(report)}).status_code, 401)

    def test_auth_csrf_logout_and_cookie(self):
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        self.assertEqual(self.client.post('/api/logout').status_code, 403)
        self.assertEqual(self.client.post('/api/logout', headers={'X-Workstation': '1', 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.post('logout').status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 401)

    def test_job_idempotency_and_project_serialization(self):
        first, second = self.session(), self.session()
        request = {'text': 'Inspect project', 'request_id': secrets.token_hex(16)}
        response = self.post(f'sessions/{first}/messages', request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(f'sessions/{first}/messages', request).json()['id'], response.json()['id'])
        self.assertEqual(self.post(f'sessions/{first}/messages', {**request, 'text': 'Changed'}).status_code, 409)
        self.assertEqual(self.post(f'sessions/{second}/messages', {**request, 'request_id': secrets.token_hex(16)}).status_code, 409)
        self.assertEqual(len(self.client.get(f'/api/sessions/{first}/events').json()), 1)

    def test_stop_is_durable_and_history_remains(self):
        sid = self.session()
        j = self.post(f'sessions/{sid}/messages', {'text': 'task', 'request_id': secrets.token_hex(16)}).json()
        self.post(f'sessions/{sid}/stop')
        self.assertEqual(db.one('SELECT stop FROM jobs WHERE id=?', (j['id'],))['stop'], 1)
        self.assertEqual(len(self.client.get(f'/api/sessions/{sid}/events').json()), 1)

    def test_path_boundaries_and_hidden_secrets(self):
        self.assertEqual(self.client.get('/api/projects/project/file', params={'path': 'main.py'}).status_code, 200)
        for path in ['../other', '/etc/hosts', '.env', 'outside']:
            self.assertEqual(self.client.get('/api/projects/project/file', params={'path': path}).status_code, 403)
        files = self.client.get('/api/projects/project/files').json()
        self.assertEqual([f['name'] for f in files], ['main.py'])
        self.assertEqual(self.post('projects', {'path': '/etc'}).status_code, 403)

    def test_approval_only_applies_to_live_waiting_job(self):
        sid = self.session()
        j = self.post(f'sessions/{sid}/messages', {'text': 'task', 'request_id': secrets.token_hex(16)}).json()
        db.execute("UPDATE jobs SET status='waiting',pid=? WHERE id=?", (os.getpid(), j['id']))
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,NULL,?)', ('approval', j['id'], 'command', '{}', time.time()))
        self.assertEqual(self.post('approvals/approval', {'decision': 'acceptForSession'}).status_code, 422)
        self.assertEqual(self.post('approvals/approval', {'decision': 'accept'}).status_code, 200)
        db.execute("UPDATE jobs SET status='completed' WHERE id=?", (j['id'],))
        self.assertEqual(self.post('approvals/approval', {'decision': 'accept'}).status_code, 409)

    def test_reconcile_marks_dead_worker_without_replaying(self):
        sid = self.session()
        j = self.post(f'sessions/{sid}/messages', {'text': 'task', 'request_id': secrets.token_hex(16)}).json()
        db.execute("UPDATE jobs SET status='running',pid=99999999 WHERE id=?", (j['id'],))
        db.reconcile()
        self.assertEqual(db.one('SELECT status FROM jobs WHERE id=?', (j['id'],))['status'], 'interrupted')
        self.assertEqual(len(db.query('SELECT * FROM jobs')), 1)

    def test_questions_validate_ids_and_never_store_secret_answers(self):
        sid = self.session()
        j = self.post(f'sessions/{sid}/messages', {'text': 'task', 'request_id': secrets.token_hex(16)}).json()
        db.execute("UPDATE jobs SET status='waiting',pid=? WHERE id=?", (os.getpid(), j['id']))
        payload = json.dumps({'questions': [{'id': 'choice', 'question': 'Choose'}]})
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,NULL,?)', ('question', j['id'], 'item/tool/requestUserInput', payload, time.time()))
        self.assertEqual(self.post('approvals/question', {'decision': 'accept'}).status_code, 409)
        self.assertEqual(self.post('approvals/question/answers', {'answers': {'wrong': ['A']}}).status_code, 400)
        self.assertEqual(self.post('approvals/question/answers', {'answers': {'choice': ['A']}}).status_code, 200)
        payload = json.dumps({'questions': [{'id': 'secret', 'isSecret': True}]})
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,NULL,?)', ('secret', j['id'], 'item/tool/requestUserInput', payload, time.time()))
        self.assertEqual(self.post('approvals/secret/answers', {'answers': {'secret': ['never-store-this']}}).status_code, 400)
        self.assertIsNone(db.one('SELECT decision FROM approvals WHERE id=?', ('secret',))['decision'])

    def test_event_cursor_does_not_duplicate(self):
        sid = self.session()
        first = db.event(sid, None, 'notice', {'text': 'first'})
        second = db.event(sid, None, 'notice', {'text': 'second'})
        values = self.client.get(f'/api/sessions/{sid}/events?after={first}').json()
        self.assertEqual([v['id'] for v in values], [second])

    def test_region_failure_is_not_reported_as_missing_login(self):
        message = 'Eligibility check failed: Your current account is not eligible for Antigravity, because it is not currently available in your location.'
        result = engines.record('antigravity', engines.classify_error(message))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['code'], 'region_restricted')
        value = self.client.get('/api/state').json()['engines']['antigravity']
        self.assertEqual(value['status'], 'blocked')
        self.assertNotIn('首次使用', value['detail'])

    def test_connection_probe_is_only_user_triggered(self):
        from unittest.mock import AsyncMock
        expected = {'status': 'ready', 'code': None, 'detail': '真实模型调用成功'}
        with patch('workstation.engines.check_antigravity', new=AsyncMock(return_value=expected)) as check:
            self.client.get('/api/state')
            check.assert_not_awaited()
            response = self.post('engines/antigravity/check')
            self.assertEqual(response.json()['status'], 'ready')
            check.assert_awaited_once()
        self.post('logout')
        self.assertEqual(self.post('engines/antigravity/check').status_code, 401)

    def test_terminal_reuse_and_input_validation(self):
        sid = self.session()
        first = self.post(f'sessions/{sid}/terminal').json()
        self.assertEqual(first['id'], self.post(f'sessions/{sid}/terminal').json()['id'])
        self.assertEqual(self.post(f'terminals/{first["id"]}/input', {'kind': 'resize', 'data': '{"rows":-1,"cols":0}'}).status_code, 400)
        self.assertEqual(self.post(f'terminals/{first["id"]}/input', {'kind': 'data', 'data': base64.b64encode(b'pwd\r').decode()}).status_code, 200)
        self.assertEqual(len(db.query('SELECT * FROM terminal_input')), 1)

    def test_websocket_requires_auth_and_origin(self):
        sid = self.session()
        with self.assertRaises(Exception):
            with self.client.websocket_connect(f'ws://localhost:8765/api/sessions/{sid}/stream', headers={'Origin': 'https://evil.example'}):
                pass
        db.event(sid, None, 'notice', {'text': 'replay'})
        with self.client.websocket_connect(f'ws://localhost:8765/api/sessions/{sid}/stream', headers={'Origin': 'http://localhost:8765'}) as ws:
            self.assertEqual(ws.receive_json()['events'][0]['payload']['text'], 'replay')

    def test_model_effort_mode_fast_and_injection_guard(self):
        state = self.client.get('/api/state').json()
        self.assertIn('catalog', state)
        self.assertTrue(len(state['catalog']['codex']['models']) >= 1)
        self.assertIn('high', state['catalog']['efforts'])
        self.assertEqual(self.post('sessions', {'project_id': 'project', 'engine': 'codex', 'model': '--bad/flag'}).status_code, 400)

        res = self.post('sessions', {
            'project_id': 'project',
            'engine': 'codex',
            'model': 'gpt-6-astra',
            'effort': 'xhigh',
            'mode': 'plan',
            'fast': True,
        })
        self.assertEqual(res.status_code, 200)
        sid = res.json()['id']
        self.assertEqual(res.json()['model'], 'gpt-6-astra')
        self.assertEqual(res.json()['effort'], 'xhigh')
        self.assertEqual(res.json()['mode'], 'plan')
        self.assertEqual(res.json()['fast'], 1)

        msg = self.post(f'sessions/{sid}/messages', {
            'text': 'Design architecture',
            'request_id': secrets.token_hex(16),
            'model': 'gpt-6.1-sol',
            'effort': 'high',
            'mode': 'auto',
            'fast': False,
        })
        self.assertEqual(msg.status_code, 200)
        job = msg.json()
        self.assertEqual(job['model'], 'gpt-6.1-sol')
        self.assertEqual(job['effort'], 'high')
        self.assertEqual(job['mode'], 'auto')
        self.assertEqual(job['fast'], 0)
        updated = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        self.assertEqual(updated['model'], 'gpt-6.1-sol')
        self.assertEqual(updated['effort'], 'high')
        self.assertEqual(updated['mode'], 'auto')

        # Test switching to full access (danger-full-access) mode
        patch_full = self.client.patch(f'/api/sessions/{sid}', json={'mode': 'full'}, headers=self.headers)
        self.assertEqual(patch_full.status_code, 200)
        self.assertEqual(patch_full.json()['mode'], 'full')
        self.assertEqual(db.one('SELECT mode FROM sessions WHERE id=?', (sid,))['mode'], 'full')

        # Test that _codex_policy never returns approval_policy='never'
        from workstation.worker import _codex_policy
        for m in ('standard', 'full', 'auto', 'plan'):
            ap, ar, ts, tns = _codex_policy(m)
            self.assertNotEqual(ap, 'never', f'Mode {m} must never return approval_policy=never')
        full_ap, full_ar, full_ts, full_tns = _codex_policy('full')
        self.assertEqual(full_ts, 'danger-full-access')
        self.assertEqual(full_tns, {'type': 'dangerFullAccess'})
        auto_ap, auto_ar, auto_ts, auto_tns = _codex_policy('auto')
        self.assertEqual(auto_ar, 'auto_review')
        self.assertTrue(auto_tns.get('networkAccess'))

    def test_session_rename_pin_and_safe_delete(self):
        first, second = self.session(), self.session()
        patch_res = self.client.patch(f'/api/sessions/{second}', json={'title': '核心重构', 'pinned': True}, headers=self.headers)
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()['title'], '核心重构')
        self.assertEqual(patch_res.json()['pinned'], 1)
        ordered = self.client.get('/api/state').json()['sessions']
        self.assertEqual(ordered[0]['id'], second)

        job = self.post(f'sessions/{first}/messages', {'text': 'active', 'request_id': secrets.token_hex(16)}).json()
        self.assertEqual(self.client.delete(f'/api/sessions/{first}', headers=self.headers).status_code, 409)
        db.execute("UPDATE jobs SET status='completed' WHERE id=?", (job['id'],))
        self.assertEqual(self.client.delete(f'/api/sessions/{first}', headers=self.headers).status_code, 200)
        self.assertIsNone(db.one('SELECT * FROM sessions WHERE id=?', (first,)))

    def test_image_attachment_validation_and_serving(self):
        sid = self.session()
        bad_png = 'data:image/png;base64,' + base64.b64encode(b'not-a-real-png').decode()
        self.assertEqual(self.post(f'sessions/{sid}/messages', {
            'text': 'check image',
            'request_id': secrets.token_hex(16),
            'images': [bad_png],
        }).status_code, 400)

        tiny_png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        good_png = 'data:image/png;base64,' + base64.b64encode(tiny_png).decode()
        res = self.post(f'sessions/{sid}/messages', {
            'text': '',
            'request_id': secrets.token_hex(16),
            'images': [good_png],
        })
        self.assertEqual(res.status_code, 200)
        events = self.client.get(f'/api/sessions/{sid}/events').json()
        images = events[0]['payload']['images']
        self.assertEqual(len(images), 1)
        img_res = self.client.get(f'/api/sessions/{sid}/uploads/{images[0]}')
        self.assertEqual(img_res.status_code, 200)
        self.assertEqual(img_res.content, tiny_png)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}/uploads/auth.json').status_code, 404)
        self.assertIn(self.client.get(f'/api/sessions/{sid}/uploads/%2e%2e%2fauth.json').status_code, (404, 405))

    def test_search_files_respects_boundaries(self):
        sub = self.project / 'src'
        sub.mkdir()
        (sub / 'helper.py').write_text('x = 1\n')
        res = self.client.get('/api/projects/project/search_files', params={'q': 'help'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([item['path'] for item in res.json()], ['src/helper.py'])
        all_files = self.client.get('/api/projects/project/search_files').json()
        self.assertNotIn('.env', [item['name'] for item in all_files])

    def _setup_mock_codex_home(self):
        codex_dir = Path(self.temp.name) / 'mock_codex'
        codex_dir.mkdir()
        (codex_dir / 'sessions/2026/11/02').mkdir(parents=True)
        (codex_dir / 'thread-writer-locks').mkdir()

        extra_ws = Path(self.temp.name) / 'codex_workspace'
        extra_ws.mkdir()
        subrepo = extra_ws / 'subrepo'
        subrepo.mkdir()
        (subrepo / 'app.py').write_text('print("synced")\n')

        (codex_dir / 'config.toml').write_text(
            f'[projects."{extra_ws}"]\ntrust_level = "trusted"\n',
            encoding='utf-8',
        )

        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.executescript('''
            CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, metadata TEXT, position INTEGER);
            CREATE TABLE project_roots(project_id TEXT, position INTEGER, path TEXT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
            CREATE TABLE threads(
                id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER,
                source TEXT, thread_source TEXT, originator TEXT, agent_path TEXT, cwd TEXT,
                title TEXT, name TEXT, preview TEXT, first_user_message TEXT, archived INTEGER,
                model TEXT, reasoning_effort TEXT, is_pinned INTEGER, tokens_used INTEGER, git_branch TEXT
            );
            ''')
            sconn.execute('INSERT INTO projects VALUES(?,?,?,?)', ('p1', 'Codex区', '{}', 0))
            sconn.execute('INSERT INTO project_roots VALUES(?,?,?)', ('p1', 0, str(extra_ws)))

        with contextlib.closing(sqlite3.connect(codex_dir / 'thread_history_1.sqlite')) as thconn, thconn:
            thconn.executescript('''
            CREATE TABLE thread_turns(
                thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT,
                started_at INTEGER, completed_at INTEGER
            );
            CREATE TABLE thread_items(
                thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
                created_at_ms INTEGER, item_json TEXT, item_type TEXT, updated_at_ordinal INTEGER
            );
            CREATE TABLE thread_history_projection_state(
                thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER
            );
            ''')

        cfg = db.config()
        cfg['codex_home'] = str(codex_dir)
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))
        return codex_dir, extra_ws, subrepo

    def test_codex_project_and_thread_history_sync(self):
        codex_dir, extra_ws, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-t1.jsonl'
        rollout_path.write_text('', encoding='utf-8')

        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            # Main user thread in subrepo
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-user-1', str(rollout_path), 1793600000, 1793600100, 'vscode', 'user', 'Codex Desktop',
                 None, str(subrepo), '原始长标题', '桌面端智能标题', '', '', 0, 'gpt-6.1-sol', 'high', 1, 12345, 'main'),
            )
            # Automation thread
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-auto-1', '', 1793500000, 1793500050, 'vscode', 'automation', 'Codex Desktop',
                 None, str(extra_ws), 'Automation: 额度巡检', None, '', '', 0, 'gpt-6-luna', 'low', 0, 500, None),
            )
            # Internal subagent child thread (must be excluded)
            sconn.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('tid-user-1', 'tid-sub-1', 'open'))
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-sub-1', '', 1793600010, 1793600020, '{"subagent":{}}', 'subagent', 'Codex Desktop',
                 '/root/helper', str(subrepo), '', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 100, None),
            )

        with contextlib.closing(sqlite3.connect(codex_dir / 'thread_history_1.sqlite')) as thconn, thconn:
            thconn.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', ('tid-user-1', 'turn-1', 1, 'completed', 100, 120))
            thconn.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', ('tid-user-1', 0, 10))
            items = [
                ('u-1', 2, 'userMessage', {'type': 'userMessage', 'id': 'u-1', 'content': [{'type': 'text', 'text': '重构服务模块'}]}),
                ('r-empty', 3, 'reasoning', {'type': 'reasoning', 'id': 'r-empty', 'summary': [], 'content': []}),
                ('r-1', 4, 'reasoning', {'type': 'reasoning', 'id': 'r-1', 'summary': ['分析依赖边界'], 'content': []}),
                ('c-1', 5, 'commandExecution', {'type': 'commandExecution', 'id': 'c-1', 'command': 'pytest -q', 'aggregatedOutput': '4 passed\n', 'exitCode': 0, 'durationMs': 420}),
                ('f-1', 6, 'fileChange', {'type': 'fileChange', 'id': 'f-1', 'changes': [{'path': str(subrepo / 'app.py'), 'kind': {'type': 'update'}, 'diff': '@@ -1 +1 @@\n-old\n+new\n'}]}),
                ('a-1', 7, 'agentMessage', {'type': 'agentMessage', 'id': 'a-1', 'text': '已完成模块重构并通过测试。'}),
            ]
            for iid, ord_num, itype, obj in items:
                thconn.execute(
                    'INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                    ('tid-user-1', 'turn-1', iid, ord_num, 1793600000000 + ord_num * 1000, json.dumps(obj, ensure_ascii=False), itype, ord_num),
                )

        state = self.client.get('/api/state').json()
        self.assertTrue(state['codex_sync']['enabled'])
        proj_names = {p['name'] for p in state['projects']}
        self.assertIn('codex_workspace', proj_names)
        self.assertIn('subrepo', proj_names)

        native_ids = {s['native_id']: s for s in state['sessions'] if s.get('native_id')}
        self.assertIn('tid-user-1', native_ids)
        self.assertIn('tid-auto-1', native_ids)
        self.assertNotIn('tid-sub-1', native_ids)

        user_sess = native_ids['tid-user-1']
        self.assertEqual(user_sess['title'], '桌面端智能标题')
        self.assertEqual(user_sess['model'], 'gpt-6.1-sol')
        self.assertEqual(user_sess['effort'], 'high')
        self.assertEqual(user_sess['pinned'], 1)
        self.assertEqual(user_sess['thread_kind'], 'user')
        self.assertEqual(native_ids['tid-auto-1']['thread_kind'], 'automation')

        evs = self.client.get(f"/api/sessions/{user_sess['id']}/events").json()
        user_evs = [e for e in evs if e['kind'] == 'user']
        self.assertEqual(len(user_evs), 1)
        self.assertEqual(user_evs[0]['payload']['text'], '重构服务模块')

        codex_items = [
            e['payload']['params']['item']['id']
            for e in evs
            if e['kind'] == 'codex' and e['payload'].get('method') == 'item/completed'
        ]
        # Empty encrypted reasoning r-empty is skipped; real reasoning, command, fileChange, agentMessage are preserved.
        self.assertEqual(codex_items, ['r-1', 'c-1', 'f-1', 'a-1'])

    def test_codex_live_incremental_rollout_tail_and_zero_duplication(self):
        codex_dir, _, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-mixed.jsonl'
        turn1_line = json.dumps({
            'ordinal': 1,
            'type': 'event_msg',
            'payload': {'type': 'item_completed', 'item': {'type': 'UserMessage', 'id': 'u-web-1', 'content': [{'type': 'text', 'text': '来自网页端的第一轮'}]}},
        }, ensure_ascii=False) + '\n'
        rollout_path.write_text(turn1_line, encoding='utf-8')
        turn1_bytes = rollout_path.stat().st_size

        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-mixed', str(rollout_path), 1793600000, 1793600100, 'vscode', 'user', 'walking_workstation',
                 None, str(subrepo), '混合会话', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 0, None),
            )
        with contextlib.closing(sqlite3.connect(codex_dir / 'thread_history_1.sqlite')) as thconn, thconn:
            thconn.execute('INSERT INTO thread_history_projection_state VALUES(?,?,?)', ('tid-mixed', turn1_bytes, 5))
            thconn.execute(
                'INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                ('tid-mixed', 't1', 'u-web-1', 1, 1793600001000,
                 json.dumps({'type': 'userMessage', 'id': 'u-web-1', 'content': [{'type': 'text', 'text': '来自网页端的第一轮'}]}, ensure_ascii=False),
                 'userMessage', 1),
            )
            thconn.execute(
                'INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                ('tid-mixed', 't1', 'm-web-1', 2, 1793600002000,
                 json.dumps({'type': 'agentMessage', 'id': 'm-web-1', 'text': '网页端第一轮回复'}, ensure_ascii=False),
                 'agentMessage', 2),
            )

        # Simulate pre-existing workstation session that already executed Turn 1 locally.
        self.post('codex/sync')
        sess = db.one("SELECT * FROM sessions WHERE native_id='tid-mixed'")
        db.execute('UPDATE sessions SET codex_synced_ordinal=-1, codex_rollout_offset=0 WHERE id=?', (sess['id'],))
        db.event(sess['id'], 'job1', 'user', {'text': '来自网页端的第一轮'})
        db.event(sess['id'], 'job1', 'codex', {'method': 'item/completed', 'params': {'item': {'type': 'userMessage', 'id': 'u-web-1'}}})
        db.event(sess['id'], 'job1', 'codex', {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'id': 'm-web-1', 'text': '网页端第一轮回复'}}})

        # Now Mac Codex Desktop appends Turn 2 directly to rollout-*.jsonl beyond projected offset!
        mac_lines = [
            {'ordinal': 6, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't2'}},
            {'ordinal': 7, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'UserMessage', 'id': 'u-mac-2', 'content': [{'type': 'text', 'text': '在 Mac 桌面端继续追问'}]}}},
            {'ordinal': 8, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'id': 'c-mac-2', 'command': ['/bin/zsh', '-lc', 'ls -la'], 'aggregated_output': 'app.py\n', 'exit_code': 0, 'duration': {'secs': 0, 'nanos': 5000000}}}},
            {'ordinal': 9, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'm-mac-2', 'content': [{'type': 'Text', 'text': 'Mac 桌面端第二轮回复'}]}}},
            {'ordinal': 10, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': 4096, 'reasoning_output_tokens': 512}, 'last_token_usage': {'total_tokens': 2048}, 'model_context_window': 475000}}},
            {'ordinal': 11, 'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't2'}},
        ]
        with rollout_path.open('a', encoding='utf-8') as f:
            for item in mac_lines:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')

        evs = self.client.get(f"/api/sessions/{sess['id']}/events").json()
        user_texts = [e['payload']['text'] for e in evs if e['kind'] == 'user']
        self.assertEqual(user_texts, ['来自网页端的第一轮', '在 Mac 桌面端继续追问'])

        agent_texts = [
            e['payload']['params']['item']['text']
            for e in evs
            if e['kind'] == 'codex'
            and e['payload'].get('method') == 'item/completed'
            and e['payload']['params']['item']['type'] == 'agentMessage'
        ]
        self.assertEqual(agent_texts, ['网页端第一轮回复', 'Mac 桌面端第二轮回复'])

    def test_codex_external_writer_lock_and_dismiss_restore(self):
        codex_dir, _, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-lock.jsonl'
        rollout_path.write_text(
            json.dumps({'ordinal': 1, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't-live'}}) + '\n',
            encoding='utf-8',
        )
        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-locked', str(rollout_path), 1793600000, int(time.time()), 'vscode', 'user', 'Codex Desktop',
                 None, str(subrepo), '本机运行中会话', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 0, None),
            )

        lock_file = codex_dir / 'thread-writer-locks/tid-locked.lock'
        lock_file.write_text('', encoding='utf-8')
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = self.client.get('/api/state').json()
            target = next(s for s in state['sessions'] if s['native_id'] == 'tid-locked')
            self.assertTrue(target['external_active'])
            self.assertTrue(target['writer_locked'])

            # Ensure codex_bin check passes in test environment so we reach the writer-lock guard.
            fake_bin = Path(self.temp.name) / 'fake-codex'
            fake_bin.write_text('#!/bin/sh\n', encoding='utf-8')
            cfg = db.config()
            cfg['codex_bin'] = str(fake_bin)
            db.write_private(db.DATA / 'config.json', json.dumps(cfg))

            conflict = self.post(f"sessions/{target['id']}/messages", {'text': '插话', 'request_id': secrets.token_hex(16)})
            self.assertEqual(conflict.status_code, 409)
            self.assertIn('Mac 本机 Codex 正在执行', conflict.json()['detail'])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        # Complete the turn on Mac; verify lock clears and dismiss/restore works.
        with rollout_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'ordinal': 2, 'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't-live'}}) + '\n')

        unlocked_state = self.client.get('/api/state').json()
        unlocked_target = next(s for s in unlocked_state['sessions'] if s['native_id'] == 'tid-locked')
        self.assertFalse(unlocked_target['external_active'])
        self.assertFalse(unlocked_target['writer_locked'])

        self.assertEqual(self.client.delete(f"/api/sessions/{unlocked_target['id']}", headers=self.headers).status_code, 200)
        after_del = self.client.get('/api/state').json()
        self.assertNotIn('tid-locked', [s.get('native_id') for s in after_del['sessions']])

        self.post('codex/sync', {'restore_dismissed': True})
        restored = self.client.get('/api/state').json()
        self.assertIn('tid-locked', [s.get('native_id') for s in restored['sessions']])

    def test_codex_idle_persistent_writer_lock_allows_input_and_forks(self):
        import asyncio
        from unittest.mock import patch
        from workstation import worker

        codex_dir, _, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-idle-lock.jsonl'
        rollout_path.write_text(
            '\n'.join([
                json.dumps({'ordinal': 1, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't-1'}}),
                json.dumps({'ordinal': 2, 'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't-1'}}),
            ]) + '\n',
            encoding='utf-8',
        )
        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-idle-locked', str(rollout_path), 1793600000, int(time.time()), 'vscode', 'user', 'Codex Desktop',
                 None, str(subrepo), '原生端已完成但仍持锁', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 0, None),
            )

        lock_file = codex_dir / 'thread-writer-locks/tid-idle-locked.lock'
        lock_file.write_text('', encoding='utf-8')
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Even while native client holds flock, completed turn must NOT lock Rove UI/API input.
            state = self.client.get('/api/state').json()
            target = next(s for s in state['sessions'] if s['native_id'] == 'tid-idle-locked')
            self.assertFalse(target['external_active'])
            self.assertFalse(target['writer_locked'])

            fake_bin = Path(self.temp.name) / 'fake-codex'
            fake_bin.write_text('#!/bin/sh\n', encoding='utf-8')
            cfg = db.config()
            cfg['codex_bin'] = str(fake_bin)
            db.write_private(db.DATA / 'config.json', json.dumps(cfg))

            with patch('workstation.server.spawn'):
                res = self.post(f"sessions/{target['id']}/messages", {'text': '继续任务', 'request_id': secrets.token_hex(16)})
            self.assertEqual(res.status_code, 200, res.text)
            job = res.json()

            calls = []
            forked_rollout = str(codex_dir / 'sessions/2026/11/15/rollout-forked.jsonl')

            class FakeRPC:
                def __init__(self, binary, notify, request):
                    self.notify = notify
                    self.reader = asyncio.create_task(asyncio.sleep(0))

                async def start(self):
                    return {}

                async def call(self, method, params=None, timeout=60):
                    calls.append((method, params))
                    if method == 'thread/fork':
                        return {'thread': {'id': 'tid-forked', 'path': forked_rollout}}
                    if method == 'turn/start':
                        await self.notify('turn/completed', {'turn': {'id': 'turn-new', 'status': 'completed'}})
                        return {'turn': {'id': 'turn-new'}}
                    raise AssertionError(f'Unexpected RPC call: {method}')

                async def close(self):
                    if not self.reader.done():
                        self.reader.cancel()

            session_row = db.one('SELECT * FROM sessions WHERE id=?', (target['id'],))
            project_row = db.one('SELECT * FROM projects WHERE id=?', (session_row['project_id'],))
            with patch.object(worker, 'CodexRPC', FakeRPC):
                outcome = asyncio.run(worker.run_codex(job, session_row, project_row))
            self.assertEqual(outcome['status'], 'completed')
            self.assertEqual([c[0] for c in calls], ['thread/fork', 'turn/start'])
            self.assertEqual(calls[0][1]['threadId'], 'tid-idle-locked')

            updated = db.one('SELECT * FROM sessions WHERE id=?', (target['id'],))
            self.assertEqual(updated['native_codex_id'], 'tid-forked')
            self.assertEqual(updated['rollout_path'], forked_rollout)
            self.assertEqual(updated['codex_rollout_offset'], 0)
            self.assertTrue(db.one('SELECT 1 FROM dismissed_codex_threads WHERE native_id=?', ('tid-idle-locked',)))

            # Next codex sync must not re-import the superseded locked thread as a duplicate session.
            from workstation import codex_sync
            codex_sync.sync_projects_and_threads(force=True)
            all_sessions = db.query('SELECT * FROM sessions')
            self.assertFalse(any(s.get('native_codex_id') == 'tid-idle-locked' for s in all_sessions))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_state_merge_preserves_codex_external_active_with_agy_id(self):
        from unittest.mock import patch
        codex_dir, _, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-dual.jsonl'
        rollout_path.write_text(
            json.dumps({'ordinal': 1, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't-dual'}}) + '\n',
            encoding='utf-8',
        )
        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-dual', str(rollout_path), 1793600000, int(time.time()), 'vscode', 'user', 'Codex Desktop',
                 None, str(subrepo), '双引擎活跃锁测试', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 0, None),
            )
        state = self.client.get('/api/state').json()
        target = next(s for s in state['sessions'] if s.get('native_codex_id') == 'tid-dual')
        db.execute('UPDATE sessions SET native_antigravity_id=? WHERE id=?', ('agy-idle-conv', target['id']))
        lock_file = codex_dir / 'thread-writer-locks/tid-dual.lock'
        lock_file.write_text('', encoding='utf-8')
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('workstation.antigravity_sync.live_antigravity_states', return_value={target['id']: {'external_active': False, 'writer_locked': False}}):
                st2 = self.client.get('/api/state').json()
            t2 = next(s for s in st2['sessions'] if s['id'] == target['id'])
            self.assertTrue(t2['external_active'])
            self.assertTrue(t2['writer_locked'])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_antigravity_hub_bypasses_cli_region_restriction_and_streams_steps(self):
        import asyncio
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from workstation import worker

        hub_state = {
            'trajectories': {},
            'steps': {},
            'deleted': [],
            'last_plan_model': None,
        }

        class MockHubHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/':
                    body = b'<script>window.__APP_CONFIG__ = {"productName":"antigravity","csrfToken":"test-csrf-123"};</script>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if self.headers.get('x-codeium-csrf-token') != 'test-csrf-123':
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get('Content-Length', '0'))
                payload = json.loads(self.rfile.read(length) or b'{}')
                method = self.path.rsplit('/', 1)[-1]
                out = {}
                if method == 'GetUserStatus':
                    out = {
                        'userStatus': {
                            'name': 'tester',
                            'email': 'tester@example.com',
                            'planStatus': {'planInfo': {'planName': 'Pro', 'teamsTier': 'TEAMS_TIER_PRO'}},
                        }
                    }
                elif method == 'GetCascadeModelConfigData':
                    out = {
                        'clientModelConfigs': [
                            {
                                'modelId': 'gemini-4-argon-high',
                                'label': 'Gemini 4 Argon (High)',
                                'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M417'},
                                'supportsImages': True,
                                'quotaInfo': {'remainingFraction': 0.8},
                            },
                            {
                                'modelId': 'gemini-4-argon-medium',
                                'label': 'Gemini 4 Argon (Medium)',
                                'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M418'},
                                'supportsImages': True,
                            },
                            {
                                'modelId': 'gemini-4-argon-low',
                                'label': 'Gemini 4 Argon (Low)',
                                'modelOrAlias': {'model': 'MODEL_PLACEHOLDER_M419'},
                                'supportsImages': True,
                            },
                        ]
                    }
                elif method == 'StartCascade':
                    cid = payload.get('cascadeId') or 'hub-cid-1'
                    hub_state['trajectories'][cid] = {'status': 'CASCADE_RUN_STATUS_IDLE', 'trajectoryId': f'traj-{cid}'}
                    hub_state['steps'][cid] = []
                    out = {'cascadeId': cid}
                elif method == 'SendUserCascadeMessage':
                    cid = payload['cascadeId']
                    hub_state['last_plan_model'] = ((payload.get('cascadeConfig') or {}).get('plannerConfig') or {}).get('planModel')
                    text = ((payload.get('items') or [{}])[0]).get('text', '')
                    hub_state['steps'][cid].extend([
                        {'type': 'CORTEX_STEP_TYPE_USER_INPUT', 'status': 'CORTEX_STEP_STATUS_DONE', 'userInput': {'userResponse': text}},
                        {
                            'type': 'CORTEX_STEP_TYPE_RUN_COMMAND',
                            'status': 'CORTEX_STEP_STATUS_DONE',
                            'runCommand': {'commandLine': 'pytest -q', 'cwd': str(self.test_cwd), 'exitCode': 0, 'combinedOutput': {'full': '1 passed\n'}},
                        },
                        {
                            'type': 'CORTEX_STEP_TYPE_PLANNER_RESPONSE',
                            'status': 'CORTEX_STEP_STATUS_DONE',
                            'plannerResponse': {
                                'thinking': 'Checking local hub bridge',
                                'response': 'WORKSTATION_ANTIGRAVITY_OK' if 'WORKSTATION_ANTIGRAVITY_OK' in text else f'Hub reply: {text}',
                            },
                        },
                    ])
                    hub_state['trajectories'][cid]['status'] = 'CASCADE_RUN_STATUS_IDLE'
                    out = {}
                elif method == 'GetAllCascadeTrajectories':
                    out = {'trajectorySummaries': hub_state['trajectories']}
                elif method == 'GetCascadeTrajectorySteps':
                    cid = payload['cascadeId']
                    offset = int(payload.get('stepOffset') or 0)
                    out = {'steps': hub_state['steps'].get(cid, [])[offset:]}
                elif method == 'DeleteCascadeTrajectory':
                    cid = payload['cascadeId']
                    hub_state['deleted'].append(cid)
                    hub_state['trajectories'].pop(cid, None)
                    hub_state['steps'].pop(cid, None)
                    out = {}
                raw = json.dumps(out).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        MockHubHandler.test_cwd = self.project
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), MockHubHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # Seed a prior CLI region_restricted failure, then point config to local Hub.
            engines.record('antigravity', engines.classify_error('Your current account is not eligible for Antigravity, because it is not currently available in your location.'))
            self.assertEqual(engines.state('antigravity')['code'], 'region_restricted')

            cfg = db.config()
            cfg['antigravity_hub_url'] = f'http://127.0.0.1:{port}'
            db.write_private(db.DATA / 'config.json', json.dumps(cfg))

            # Model refresh heals stale region_restricted state and enables image support.
            refreshed = self.post('models/refresh')
            self.assertEqual(refreshed.status_code, 200)
            agy_models = refreshed.json()['antigravity']['models']
            self.assertTrue(any(m['id'] == 'gemini-4-argon-high' and m['supportsImage'] for m in agy_models))
            self.assertEqual(engines.state('antigravity')['status'], 'ready')

            # Explicit connection probe runs real roundtrip and cleans up ephemeral cascade.
            probe = self.post('engines/antigravity/check')
            self.assertEqual(probe.status_code, 200)
            self.assertEqual(probe.json()['status'], 'ready')
            self.assertEqual(len(hub_state['deleted']), 1)

            # Create an Antigravity session with medium model + high effort override -> resolves to M417 (High).
            sess = self.post('sessions', {
                'project_id': 'project',
                'engine': 'antigravity',
                'model': 'gemini-4-argon-medium',
                'effort': 'high',
                'mode': 'standard',
            }).json()
            job_res = self.post(f"sessions/{sess['id']}/messages", {
                'text': '用 Antigravity 分析项目',
                'request_id': secrets.token_hex(16),
                'model': 'gemini-4-argon-medium',
                'effort': 'high',
            })
            self.assertEqual(job_res.status_code, 200)
            job_id = job_res.json()['id']

            asyncio.run(worker.run(job_id))
            self.assertEqual(db.one('SELECT status FROM jobs WHERE id=?', (job_id,))['status'], 'completed')
            self.assertEqual(hub_state['last_plan_model'], 'MODEL_PLACEHOLDER_M417')

            updated_sess = db.one('SELECT native_id FROM sessions WHERE id=?', (sess['id'],))
            self.assertTrue(updated_sess['native_id'])

            evs = self.client.get(f"/api/sessions/{sess['id']}/events").json()
            step_kinds = [e['payload'].get('step_kind') for e in evs if e['kind'] == 'antigravity' and e['payload'].get('event') == 'agy_step']
            self.assertIn('command', step_kinds)
            self.assertIn('reasoning', step_kinds)
            self.assertIn('agent', step_kinds)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_antigravity_content_sync_and_transcript_events(self):
        from workstation import antigravity_sync

        mock_agy = Path(self.temp.name) / 'mock_gemini_antigravity'
        mock_agy.mkdir(parents=True)
        summaries_db = mock_agy / 'conversation_summaries.db'

        with contextlib.closing(sqlite3.connect(summaries_db)) as conn, conn:
            conn.execute('''
            CREATE TABLE conversation_summaries (
                conversation_id text PRIMARY KEY,
                title text NOT NULL,
                preview text NOT NULL,
                step_count integer NOT NULL,
                last_modified_time datetime NOT NULL,
                last_user_input_time datetime NOT NULL,
                workspace_uris text NOT NULL,
                not_fully_idle numeric NOT NULL DEFAULT 0
            )
            ''')
            now_iso = '2026-11-02 12:00:00.000000+00:00'
            conn.execute(
                'INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?)',
                (
                    'conv-agy-001', 'Antigravity 项目架构分析', '架构分析预览', 2,
                    now_iso, now_iso, json.dumps([f'file://{self.project}']), 1,
                ),
            )
            conn.execute(
                'INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?)',
                (
                    'conv-agy-002', '历史辅助会话', '历史预览', 1,
                    now_iso, now_iso, json.dumps([f'file://{self.project}']), 0,
                ),
            )

        # Write transcript for conv-agy-001
        transcript_dir = mock_agy / 'brain' / 'conv-agy-001' / '.system_generated' / 'logs'
        transcript_dir.mkdir(parents=True)
        transcript_file = transcript_dir / 'transcript.jsonl'
        transcript_file.write_text(
            json.dumps({
                'step_index': 0,
                'source': 'USER_EXPLICIT',
                'type': 'USER_INPUT',
                'status': 'DONE',
                'created_at': '2026-11-02T12:00:00Z',
                'content': '<USER_REQUEST>\n请分析项目架构并运行自动化测试\n</USER_REQUEST>',
            }) + '\n' +
            json.dumps({
                'step_index': 1,
                'source': 'MODEL',
                'type': 'PLANNER_RESPONSE',
                'status': 'DONE',
                'created_at': '2026-11-02T12:00:05Z',
                'thinking': 'Analyzing project files and test coverage',
                'content': '已完成项目架构分析，发现结构完整。',
                'tool_calls': [
                    {'name': 'run_command', 'arguments': {'CommandLine': 'pytest -q'}},
                    {'name': 'read_file', 'arguments': {'Path': str(self.project / 'main.py')}},
                ],
            }) + '\n'
        )

        cfg = db.config()
        cfg['antigravity_home'] = str(mock_agy)
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))
        db.execute("DELETE FROM sessions WHERE engine='antigravity'")

        # 1. Sync Antigravity projects and sessions
        sync_res = self.post('antigravity/sync')
        self.assertEqual(sync_res.status_code, 200)
        data = sync_res.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['synced_count'], 2)

        # 2. Check /api/state: conv-agy-001 must have external_active=True because not_fully_idle=1
        st = self.client.get('/api/state').json()
        s1 = next((s for s in st['sessions'] if s.get('native_id') == 'conv-agy-001'), None)
        s2 = next((s for s in st['sessions'] if s.get('native_id') == 'conv-agy-002'), None)
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        self.assertTrue(s1['external_active'])
        self.assertFalse(s2['external_active'])
        self.assertEqual(st['antigravity_sync']['enabled'], True)
        self.assertEqual(st['antigravity_sync']['external_active_count'], 1)

        # 3. Read events for s1
        evs_res = self.client.get(f"/api/sessions/{s1['id']}/events")
        self.assertEqual(evs_res.status_code, 200)
        evs = evs_res.json()
        user_ev = next((e for e in evs if e['kind'] == 'user'), None)
        self.assertIsNotNone(user_ev)
        self.assertIn('请分析项目架构并运行自动化测试', user_ev['payload']['text'])

        agy_steps = [e['payload'] for e in evs if e['kind'] == 'antigravity' and e['payload'].get('event') == 'agy_step']
        step_kinds = [p.get('step_kind') for p in agy_steps]
        self.assertIn('reasoning', step_kinds)
        self.assertIn('agent', step_kinds)
        self.assertIn('command', step_kinds)
        self.assertIn('tool', step_kinds)

        # Second sync must add 0 duplicate events
        events_added = antigravity_sync.sync_antigravity_session_events(s1['id'])
        self.assertEqual(events_added, 0)

        # 4. Safe delete / dismiss of s2
        del_res = self.client.delete(f"/api/sessions/{s2['id']}", headers=self.headers)
        self.assertEqual(del_res.status_code, 200)
        dismissed = db.query('SELECT native_id FROM dismissed_antigravity_threads WHERE native_id=?', ('conv-agy-002',))
        self.assertEqual(len(dismissed), 1)

        # Ordinary sync does not restore dismissed s2
        self.post('sync')
        st2 = self.client.get('/api/state').json()
        self.assertIsNone(next((s for s in st2['sessions'] if s.get('native_id') == 'conv-agy-002'), None))

        # Restore dismissed restores s2
        self.post('sync', {'restore_dismissed': True})
        st3 = self.client.get('/api/state').json()
        self.assertIsNotNone(next((s for s in st3['sessions'] if s.get('native_id') == 'conv-agy-002'), None))

    def test_codex_and_antigravity_quotas_endpoint(self):
        from workstation import quotas

        mock_codex_data = {
            'status': 'ready',
            'plan_type': 'PLUS',
            'limit_id': 'codex',
            'ordinary_usage_allowed': True,
            'primary': {
                'name': '5小时额度',
                'window_duration_mins': 300,
                'used_percent': 100,
                'remaining_percent': 0,
                'resets_at': int(time.time() + 5400),
                'reset_time_formatted': '今日 16:58',
                'countdown_seconds': 5400,
                'countdown_text': '1小时 30分',
            },
            'secondary': {
                'name': '周额度',
                'window_duration_mins': 10080,
                'used_percent': 92,
                'remaining_percent': 8,
                'resets_at': int(time.time() + 160000),
                'reset_time_formatted': '11月04日 20:36',
                'countdown_seconds': 160000,
                'countdown_text': '1天 20小时',
            },
            'reset_credits': {
                'available_count': 3,
                'credits': [
                    {'id': 'credit_1', 'title': 'Full reset (Weekly + 5 hr)', 'status': 'available'},
                ],
            },
        }

        mock_agy_data = {
            'status': 'ready',
            'account_email': 'tester@gmail.com',
            'account_name': 'Tester',
            'plan_name': 'Pro',
            'tier_name': 'Google AI Pro',
            'credits': {
                'available_prompt_credits': 500,
                'monthly_prompt_credits': 50000,
                'available_flow_credits': 100,
                'monthly_flow_credits': 150000,
            },
            'gemini_4_argon': {
                'label': 'Gemini 4 Argon (旗舰高阶思考)',
                'model_id': 'gemini-4-argon-medium',
                'remaining_fraction': 0.4342288,
                'remaining_percent': 43,
                'used_percent': 57,
                'resets_at': int(time.time() + 580000),
                'resets_at_iso': '2026-11-09T12:33:11Z',
                'reset_time_formatted': '11月09日 20:33 (周一)',
                'countdown_text': '6天 17小时',
                'window_type': '周额度 (Weekly)',
            },
            'models': [
                {
                    'model_id': 'gemini-4-argon-medium',
                    'label': 'Gemini 4 Argon (Medium)',
                    'remaining_percent': 43,
                    'countdown_text': '6天 17小时',
                },
                {
                    'model_id': 'claude-opus-5-5-low',
                    'label': 'Claude Opus 5.5',
                    'remaining_percent': 100,
                    'countdown_text': '已刷新',
                },
            ],
        }

        with patch.object(quotas, 'get_codex_quotas', return_value=mock_codex_data), \
             patch.object(quotas, 'get_antigravity_quotas', return_value=mock_agy_data):
            res = self.client.get('/api/quotas')
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn('codex', data)
            self.assertIn('antigravity', data)

            # Check Codex quotas
            codex_q = data['codex']
            self.assertEqual(codex_q['plan_type'], 'PLUS')
            self.assertEqual(codex_q['primary']['name'], '5小时额度')
            self.assertEqual(codex_q['primary']['used_percent'], 100)
            self.assertEqual(codex_q['secondary']['name'], '周额度')
            self.assertEqual(codex_q['secondary']['remaining_percent'], 8)
            self.assertEqual(codex_q['reset_credits']['available_count'], 3)

            # Check Antigravity & Gemini 4 Argon
            agy_q = data['antigravity']
            self.assertEqual(agy_q['account_email'], 'tester@gmail.com')
            self.assertEqual(agy_q['plan_name'], 'Pro')
            argon = agy_q['gemini_4_argon']
            self.assertEqual(argon['remaining_percent'], 43)
            self.assertEqual(argon['used_percent'], 57)
            self.assertEqual(argon['window_type'], '周额度 (Weekly)')
            self.assertEqual(argon['countdown_text'], '6天 17小时')

            # Check refresh endpoint
            ref_res = self.post('quotas/refresh')
            self.assertEqual(ref_res.status_code, 200)
            self.assertEqual(ref_res.json()['codex']['primary']['used_percent'], 100)

        # Verify automatic rollover when reset time has passed (resets_at <= now)
        now_ts = int(time.time())
        expired_codex_win = quotas._build_codex_window(
            {'usedPercent': 96, 'resetsAt': now_ts - 120, 'windowDurationMins': 300},
            300, '5小时额度', now_ts
        )
        self.assertEqual(expired_codex_win['used_percent'], 0)
        self.assertEqual(expired_codex_win['remaining_percent'], 100)
        self.assertEqual(expired_codex_win['resets_at'], now_ts - 120 + 18000)

        # Verify Antigravity Hub expired 5h window auto-rollover and cache invalidation
        import asyncio
        from datetime import datetime, timezone
        past_iso = datetime.fromtimestamp(now_ts - 60, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        future_weekly_iso = datetime.fromtimestamp(now_ts + 300000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        fake_hub = unittest.mock.MagicMock()
        fake_hub.get_user_status = unittest.mock.AsyncMock(return_value={
            'email': 'tester@gmail.com',
            'name': 'Tester',
            'planStatus': {'planInfo': {'planName': 'Pro'}},
            'cascadeModelConfigData': {
                'clientModelConfigs': [
                    {
                        'modelId': 'gemini-4-argon-high',
                        'label': 'Gemini 4 Argon (High)',
                        'quotaInfo': {'remainingFraction': 0.04, 'resetTime': past_iso},
                    },
                    {
                        'modelId': 'gemini-4-argon-medium',
                        'label': 'Gemini 4 Argon (Medium)',
                        'quotaInfo': {'remainingFraction': 0.14, 'resetTime': future_weekly_iso},
                    },
                    {
                        'modelId': 'gemini-3.8-flash-high',
                        'label': 'Gemini 3.8 Flash (High)',
                        'quotaInfo': {'remainingFraction': 0.0, 'resetTime': past_iso},
                    },
                ]
            },
        })
        fake_hub.get_model_configs = unittest.mock.AsyncMock(return_value=[])
        with patch('workstation.antigravity_hub.discover_hub', new=unittest.mock.AsyncMock(return_value=fake_hub)):
            agy_rolled = asyncio.run(quotas.get_antigravity_quotas(force=True))
        self.assertEqual(agy_rolled['general_flash']['remaining_percent'], 100)
        self.assertEqual(agy_rolled['general_flash']['used_percent'], 0)
        self.assertGreater(agy_rolled['general_flash']['resets_at'], now_ts)
        self.assertEqual(agy_rolled['general_flash']['weekly']['remaining_percent'], 34)
        self.assertEqual(agy_rolled['general_flash']['weekly']['status'], 'metered')
        self.assertEqual(agy_rolled['general_flash']['rolling_5h']['remaining_percent'], 100)
        self.assertEqual(agy_rolled['gemini_4_argon']['rolling_5h']['remaining_percent'], 100)
        self.assertEqual(agy_rolled['gemini_4_argon']['weekly']['remaining_percent'], 14)

        # Verify accurate live quota summary parsing with RetrieveUserQuotaSummary
        fake_hub.retrieve_user_quota_summary = unittest.mock.AsyncMock(return_value={
            'groups': [
                {
                    'displayName': 'Gemini Models',
                    'buckets': [
                        {
                            'bucketId': 'gemini-weekly',
                            'window': 'weekly',
                            'remainingFraction': 0.31,
                            'resetTime': future_weekly_iso,
                        },
                        {
                            'bucketId': 'gemini-5h',
                            'window': '5h',
                            'remainingFraction': 0.68,
                            'resetTime': future_weekly_iso,
                        },
                    ],
                },
                {
                    'displayName': 'Claude and GPT models',
                    'buckets': [
                        {
                            'bucketId': '3p-weekly',
                            'window': 'weekly',
                            'remainingFraction': 0.05,
                            'resetTime': future_weekly_iso,
                        },
                        {
                            'bucketId': '3p-5h',
                            'window': '5h',
                            'remainingFraction': 1.0,
                            'resetTime': future_weekly_iso,
                        },
                    ],
                },
                {
                    'displayName': 'Gemini 4 Argon',
                    'buckets': [
                        {
                            'bucketId': 'gemini-argon-wtu-weekly',
                            'window': 'weekly',
                            'remainingFraction': 0.14,
                            'resetTime': future_weekly_iso,
                        },
                    ],
                },
            ],
        })
        with patch('workstation.antigravity_hub.discover_hub', new=unittest.mock.AsyncMock(return_value=fake_hub)):
            agy_with_summary = asyncio.run(quotas.get_antigravity_quotas(force=True))
        self.assertEqual(agy_with_summary['general_flash']['weekly']['remaining_percent'], 31)
        self.assertEqual(agy_with_summary['general_flash']['rolling_5h']['remaining_percent'], 68)
        self.assertEqual(agy_with_summary['claude_gpt']['weekly']['remaining_percent'], 5)
        self.assertEqual(agy_with_summary['claude_gpt']['rolling_5h']['remaining_percent'], 100)
        self.assertEqual(agy_with_summary['gemini_4_argon']['weekly']['remaining_percent'], 14)
        self.assertEqual(agy_with_summary['gemini_4_argon']['rolling_5h']['remaining_percent'], 68)

    def test_gateway_reauthentication_returns_to_app_without_workstation_login(self):
        self.client.cookies.clear()
        health = self.client.get('/healthz')
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()['ok'])
        response = self.client.get('/healthz?cf_auth=1', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/api/state').status_code, 401)

    def test_pwa_manifest_service_worker_and_icons_unauthenticated(self):
        # Log out completely: browsers fetch manifest, icons, and sw.js without session cookies.
        self.assertEqual(self.post('logout').status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 401)

        manifest_res = self.client.get('/manifest.webmanifest')
        self.assertEqual(manifest_res.status_code, 200)
        self.assertIn('application/manifest+json', manifest_res.headers.get('content-type', ''))
        self.assertEqual(manifest_res.headers.get('cache-control'), 'no-cache')
        manifest = manifest_res.json()
        self.assertEqual(manifest['name'], 'Rove')
        self.assertEqual(manifest['short_name'], 'Rove')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['id'], '/')
        purposes = {(icon['sizes'], icon['purpose']) for icon in manifest['icons']}
        self.assertIn(('192x192', 'any'), purposes)
        self.assertIn(('512x512', 'any'), purposes)
        self.assertIn(('512x512', 'maskable'), purposes)
        self.assertTrue(len(manifest['shortcuts']) >= 3)

        sw_res = self.client.get('/sw.js')
        self.assertEqual(sw_res.status_code, 200)
        self.assertEqual(sw_res.headers.get('service-worker-allowed'), '/')
        self.assertEqual(sw_res.headers.get('cache-control'), 'no-cache')
        self.assertIn("addEventListener('push'", sw_res.text)
        self.assertIn("addEventListener('notificationclick'", sw_res.text)

        for png_route in ('/apple-touch-icon.png', '/pwa-192.png', '/pwa-512.png', '/pwa-maskable-512.png'):
            icon_res = self.client.get(png_route)
            self.assertEqual(icon_res.status_code, 200, png_route)
            self.assertEqual(icon_res.content[:8], b'\x89PNG\r\n\x1a\n')

    def test_web_push_vapid_ssrf_guard_encryption_and_auto_pruning(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from workstation import webpush

        st = self.client.get('/api/state').json()
        self.assertIn('web_push', st)
        pub_key = st['web_push']['public_key']
        self.assertEqual(len(webpush.b64url_decode(pub_key)), 65)
        vapid_path = db.DATA / 'vapid.json'
        self.assertTrue(vapid_path.is_file())
        self.assertEqual(vapid_path.stat().st_mode & 0o777, 0o600)

        ua_priv, ua_pub = webpush.generate_p256_keypair()
        auth_secret = os.urandom(16)
        valid_keys = {
            'p256dh': webpush.b64url_encode(ua_pub),
            'auth': webpush.b64url_encode(auth_secret),
        }

        # SSRF protection must block HTTP, loopback, private IPs, and .local hosts by default
        for blocked_ep in (
            'http://web.push.apple.com/sub1',
            'https://127.0.0.1:9999/sub1',
            'https://192.168.1.20/sub1',
            'https://internal.local/sub1',
        ):
            bad_res = self.post('push/subscribe', {'endpoint': blocked_ep, 'keys': valid_keys})
            self.assertEqual(bad_res.status_code, 400, blocked_ep)

        # Invalid off-curve P-256 public point must be rejected
        bad_point = b'\x04' + (b'\x01' * 64)
        self.assertEqual(self.post('push/subscribe', {
            'endpoint': 'https://web.push.apple.com/sub-bad',
            'keys': {'p256dh': webpush.b64url_encode(bad_point), 'auth': valid_keys['auth']},
        }).status_code, 400)

        # Spin up a local mock Web Push service endpoint (201 for /live, 410 Gone for /gone)
        received = []

        class MockPushHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                if self.path == '/gone':
                    self.send_response(410)
                    self.end_headers()
                    return
                received.append({
                    'path': self.path,
                    'headers': {k.lower(): v for k, v in self.headers.items()},
                    'body': body,
                })
                self.send_response(201)
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = HTTPServer(('127.0.0.1', 0), MockPushHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        try:
            cfg = db.config()
            cfg['allow_local_push_endpoints'] = True
            db.write_private(db.DATA / 'config.json', json.dumps(cfg))

            live_ep = f'http://127.0.0.1:{port}/live'
            gone_ep = f'http://127.0.0.1:{port}/gone'
            sub1 = self.post('push/subscribe', {
                'endpoint': live_ep,
                'keys': valid_keys,
                'device_label': 'iPhone (iOS)',
            })
            self.assertEqual(sub1.status_code, 200)
            sub2 = self.post('push/subscribe', {
                'endpoint': gone_ep,
                'keys': valid_keys,
                'device_label': '旧平板',
            })
            self.assertEqual(sub2.status_code, 200)
            self.assertEqual(sub2.json()['subscriptions_count'], 2)

            sid = self.session()
            test_res = self.post('push/test', {'session_id': sid})
            self.assertEqual(test_res.status_code, 200)
            out = test_res.json()
            self.assertEqual(out['sent'], 1)
            self.assertEqual(out['removed'], 1)
            self.assertEqual(out['web_push']['subscriptions_count'], 1)

            self.assertEqual(len(received), 1)
            req = received[0]
            self.assertEqual(req['headers'].get('content-encoding'), 'aes128gcm')
            self.assertEqual(req['headers'].get('urgency'), 'high')
            self.assertTrue(req['headers'].get('authorization', '').startswith('vapid t='))

            decrypted = json.loads(webpush.decrypt_webpush_payload(req['body'], ua_priv, ua_pub, auth_secret).decode('utf-8'))
            self.assertIn('锁屏推送已连通', decrypted['title'])
            self.assertEqual(decrypted['sessionId'], sid)
            self.assertEqual(decrypted['url'], f'./?session={sid}')

            unsub = self.post('push/unsubscribe', {'endpoint': live_ep})
            self.assertEqual(unsub.status_code, 200)
            self.assertEqual(unsub.json()['subscriptions_count'], 0)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_worker_triggers_push_on_approval_and_completion(self):
        import asyncio
        from unittest.mock import AsyncMock
        from workstation import webpush

        sid = self.session()
        sess = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        proj = db.one('SELECT * FROM projects WHERE id=?', (sess['project_id'],))
        job_id = 'job-push-verify'
        db.execute(
            'INSERT INTO jobs(id,session_id,request_id,prompt,status) VALUES(?,?,?,?,?)',
            (job_id, sid, secrets.token_hex(8), 'Deploy PWA', 'completed'),
        )
        db.event(sid, job_id, 'codex', {
            'method': 'item/completed',
            'params': {'item': {'type': 'agentMessage', 'text': '满血版 PWA 升级已部署完成！'}},
        })

        with patch.object(webpush, 'notify_all', new=AsyncMock(return_value={'ok': True, 'sent': 1})) as mock_notify:
            asyncio.run(webpush.notify_job_approval(
                sess, proj, 'app-123', 'item/commandExecution/requestApproval',
                {'command': 'git push origin main'},
            ))
            mock_notify.assert_awaited_once()
            approval_call = mock_notify.await_args.kwargs
            self.assertEqual(approval_call['kind'], 'approval')
            self.assertEqual(approval_call['approval_id'], 'app-123')
            self.assertEqual([a['action'] for a in approval_call['actions']], ['accept', 'decline'])
            self.assertTrue(approval_call['require_interaction'])
            self.assertIn('等待你的审批', approval_call['title'])
            self.assertIn('git push origin main', approval_call['body'])

            mock_notify.reset_mock()
            asyncio.run(webpush.notify_job_outcome(job_id, sess, proj, {'status': 'completed', 'error': None}))
            mock_notify.assert_awaited_once()
            done_call = mock_notify.await_args.kwargs
            self.assertEqual(done_call['kind'], 'completed')
            self.assertFalse(done_call['require_interaction'])
            self.assertIn('任务已完成', done_call['title'])
            self.assertIn('满血版 PWA 升级已部署完成', done_call['body'])

    def test_apple_watch_status_and_approval_endpoints(self):
        from workstation import quotas
        quotas._CACHE.clear()
        try:
            # 1. Deprecated GET approval and status endpoints return 410 Gone to prevent GET-based mutations
            res_status = self.client.get('/api/watch/status')
            self.assertEqual(res_status.status_code, 410)
            self.assertIn('永久下线注销', res_status.text)

            res_approve_get = self.client.get('/api/watch/approve')
            self.assertEqual(res_approve_get.status_code, 410)

            # 2. Reject active endpoints without token or with invalid token
            res_no_token = self.client.get('/api/watch/quotas')
            self.assertEqual(res_no_token.status_code, 401)

            res_bad_token = self.client.get('/api/watch/quotas?token=wrong_token')
            self.assertEqual(res_bad_token.status_code, 401)

            res_approve_no_tok = self.client.post('/api/watch/approve', json={'approval_id': 'x', 'decision': 'accept'})
            self.assertEqual(res_approve_no_tok.status_code, 401)

            # 3. Authenticated user can fetch watch config and new token
            cfg_res = self.client.get('/api/watch/config')
            self.assertEqual(cfg_res.status_code, 200)
            cfg_data = cfg_res.json()
            token = cfg_data['token']
            self.assertTrue(bool(token))
            self.assertIn(token, cfg_data['quotas_url'])
            self.assertIn(token, cfg_data['live_url'])
            self.assertIn(token, cfg_data['pending_url'])
            self.assertIn(token, cfg_data['approve_url'])
            self.assertNotIn('status_url', cfg_data)

            # 4. Query all models quotas endpoint
            quotas_res = self.client.get(f'/api/watch/quotas?token={token}')
            self.assertEqual(quotas_res.status_code, 200)
            self.assertIn('配额', quotas_res.text)

            quotas_json = self.client.get(f'/api/watch/quotas?token={token}&format=json')
            self.assertEqual(quotas_json.status_code, 200)
            self.assertIn('antigravity', quotas_json.json())

            # 5. Query live watch page and approval workflow
            live_page_res = self.client.get(f'/watch/live?token={token}')
            self.assertEqual(live_page_res.status_code, 200)
            self.assertIn('Rove Watch Live', live_page_res.text)
            self.assertIn('Watch 审批', live_page_res.text)
            self.assertIn('watch-approval-card', live_page_res.text)

            # Create a waiting job + pending approval and verify Watch pending + approve endpoints
            p_res = self.post('projects', {'path': str(self.project)})
            proj_id = p_res.json()['id']
            s_res = self.post('sessions', {'project_id': proj_id, 'engine': 'codex'})
            sid = s_res.json()['id']
            jid = secrets.token_hex(12)
            aid = secrets.token_hex(12)
            now = time.time()
            db.execute(
                "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'waiting',?,'codex')",
                (jid, sid, secrets.token_hex(8), 'run tests', now),
            )
            db.execute(
                'INSERT INTO approvals VALUES(?,?,?,?,NULL,?)',
                (aid, jid, 'item/commandExecution/requestApproval', json.dumps({'command': 'pytest -q'}), now),
            )

            pend_res = self.client.get(f'/api/watch/pending?token={token}')
            self.assertEqual(pend_res.status_code, 200)
            self.assertTrue(pend_res.json()['pending'])
            self.assertEqual(pend_res.json()['approval']['id'], aid)
            self.assertEqual(pend_res.json()['approval']['command'], 'pytest -q')

            pend_txt = self.client.get(f'/api/watch/pending?token={token}&format=text')
            self.assertEqual(pend_txt.status_code, 200)
            self.assertIn('pytest -q', pend_txt.text)

            live_state_res = self.client.get(f'/api/watch/live/state?token={token}')
            self.assertEqual(live_state_res.status_code, 200)
            self.assertIn('session_title', live_state_res.json())
            self.assertEqual(live_state_res.json()['approval']['id'], aid)

            approve_res = self.client.post(
                f'/api/watch/approve?token={token}',
                json={'approval_id': aid, 'decision': 'accept'},
            )
            self.assertEqual(approve_res.status_code, 200)
            self.assertTrue(approve_res.json()['ok'])
            self.assertEqual(db.one('SELECT decision FROM approvals WHERE id=?', (aid,))['decision'], 'accept')

            dup_res = self.client.post(
                f'/api/watch/approve?token={token}',
                json={'approval_id': aid, 'decision': 'decline'},
            )
            self.assertEqual(dup_res.status_code, 409)
            db.execute("UPDATE jobs SET status='completed', ended=? WHERE id=?", (time.time(), jid))

            # Verify 1-step Shortcuts approval without explicit approval_id auto-resolves latest pending
            jid2 = secrets.token_hex(12)
            aid2 = secrets.token_hex(12)
            db.execute(
                "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'waiting',?,'codex')",
                (jid2, sid, secrets.token_hex(8), 'git status', time.time()),
            )
            db.execute(
                'INSERT INTO approvals VALUES(?,?,?,?,NULL,?)',
                (aid2, jid2, 'item/commandExecution/requestApproval', json.dumps({'command': 'git status'}), time.time()),
            )
            auto_res = self.client.post(f'/api/watch/approve?token={token}', json={'decision': 'accept'})
            self.assertEqual(auto_res.status_code, 200)
            self.assertEqual(auto_res.json()['approval_id'], aid2)
            self.assertEqual(db.one('SELECT decision FROM approvals WHERE id=?', (aid2,))['decision'], 'accept')
            no_pending_res = self.client.post(f'/api/watch/approve?token={token}', json={'decision': 'accept'})
            self.assertEqual(no_pending_res.status_code, 409)
            db.execute("UPDATE jobs SET status='completed', ended=? WHERE id=?", (time.time(), jid2))

            # 6. Eink View endpoints
            eink_page_res = self.client.get(f'/eink?token={token}')
            self.assertEqual(eink_page_res.status_code, 200)
            self.assertIn('ROVE 墨水台', eink_page_res.text)
            self.assertIn('toolbar-actions', eink_page_res.text)
            self.assertIn('invert-flash-phase1', eink_page_res.text)

            eink_js_res = self.client.get('/eink/live.js')
            self.assertEqual(eink_js_res.status_code, 200)
            self.assertIn('cleanScreen', eink_js_res.text)
            self.assertIn('applyFontScale', eink_js_res.text)

            eink_state_res = self.client.get(f'/api/eink/state?token={token}')
            self.assertEqual(eink_state_res.status_code, 200)
            state_json = eink_state_res.json()
            self.assertIn('session_title', state_json)
            self.assertIn('project_name', state_json)
            self.assertIn('git_branch', state_json)

            # 7. Rotate watch token invalidates old token
            rot_res = self.post('watch/rotate')
            self.assertEqual(rot_res.status_code, 200)
            new_token = rot_res.json()['token']
            self.assertNotEqual(new_token, token)
            self.assertEqual(self.client.get(f'/api/watch/pending?token={token}').status_code, 401)
            self.assertEqual(self.client.get(f'/api/watch/pending?token={new_token}').status_code, 200)

            # 8. Push test with kind='approval' sends approval preview push to bound devices
            from unittest.mock import AsyncMock, patch
            with patch('workstation.webpush.notify_all', new=AsyncMock(return_value={'ok': True, 'sent': 1, 'failed': 0, 'removed': 0, 'errors': []})) as mock_push:
                push_res = self.post('push/test', {'kind': 'approval'})
            self.assertEqual(push_res.status_code, 200)
            self.assertEqual(push_res.json()['sent'], 1)
            self.assertEqual(mock_push.await_args.kwargs['kind'], 'approval')
            self.assertEqual(mock_push.await_args.kwargs['approval_id'], 'test-preview')
            self.assertEqual([a['action'] for a in mock_push.await_args.kwargs['actions']], ['accept', 'decline'])
        finally:
            quotas._CACHE.clear()

    def test_stop_cancels_external_antigravity_cascade_and_unblocks_session(self):
        from unittest.mock import patch

        mock_agy = Path(self.temp.name) / 'mock-agy-stop'
        mock_agy.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(mock_agy / 'conversation_summaries.db')) as conn, conn:
            conn.execute('''
            CREATE TABLE conversation_summaries(
                conversation_id text PRIMARY KEY,
                title text,
                preview text,
                step_count integer NOT NULL,
                last_modified_time datetime NOT NULL,
                last_user_input_time datetime NOT NULL,
                workspace_uris text NOT NULL,
                not_fully_idle numeric NOT NULL DEFAULT 0
            )
            ''')
            conn.execute(
                'INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?)',
                ('conv-agy-stuck', '后台持续进程', '预览', 4, '2026-11-15 12:00:00+00:00',
                 '2026-11-15 12:00:00+00:00', json.dumps([f'file://{self.project}']), 1),
            )

        cfg = db.config()
        cfg['antigravity_home'] = str(mock_agy)
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))

        self.assertEqual(self.post('antigravity/sync').status_code, 200)
        st = self.client.get('/api/state').json()
        s = next(x for x in st['sessions'] if x.get('native_id') == 'conv-agy-stuck')
        self.assertTrue(s['external_active'])

        hub_calls = []

        class FakeHub:
            async def call(self, method, payload=None, timeout=10.0):
                hub_calls.append((method, payload))
                return {}

        async def fake_discover(auto_launch=False):
            return FakeHub()

        with patch('workstation.antigravity_hub.discover_hub', side_effect=fake_discover):
            stop_res = self.post(f"sessions/{s['id']}/stop")
        self.assertEqual(stop_res.status_code, 200)
        self.assertIn(('CancelCascadeInvocation', {'cascadeId': 'conv-agy-stuck'}), hub_calls)

        # Even if conversation_summaries.db still has not_fully_idle=1, Rove must unblock immediately.
        st2 = self.client.get('/api/state').json()
        s2 = next(x for x in st2['sessions'] if x['id'] == s['id'])
        self.assertFalse(s2['external_active'])

        with patch('workstation.server.spawn'):
            msg_res = self.post(f"sessions/{s['id']}/messages", {'text': '继续任务', 'request_id': secrets.token_hex(16)})
        self.assertEqual(msg_res.status_code, 200, msg_res.text)
        db.execute("UPDATE jobs SET status='completed', ended=? WHERE session_id=?", (time.time(), s['id']))

        # If a brand-new external turn advances step_count later, external_active accurately reflects it again.
        with contextlib.closing(sqlite3.connect(mock_agy / 'conversation_summaries.db')) as conn, conn:
            conn.execute('UPDATE conversation_summaries SET step_count=6 WHERE conversation_id=?', ('conv-agy-stuck',))
        st3 = self.client.get('/api/state').json()
        s3 = next(x for x in st3['sessions'] if x['id'] == s['id'])
        self.assertTrue(s3['external_active'])

    def test_codex_stale_turn_without_lock_is_not_active_and_stop_unblocks_locked_turn(self):
        from unittest.mock import patch

        codex_dir, _, subrepo = self._setup_mock_codex_home()
        rollout_path = codex_dir / 'sessions/2026/11/02/rollout-stale-or-locked.jsonl'
        rollout_path.write_text(
            json.dumps({'ordinal': 1, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't-bg'}}) + '\n',
            encoding='utf-8',
        )
        with contextlib.closing(sqlite3.connect(codex_dir / 'state_5.sqlite')) as sconn, sconn:
            sconn.execute(
                'INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('tid-bg-stop', str(rollout_path), 1793600000, int(time.time()), 'vscode', 'user', 'Codex Desktop',
                 None, str(subrepo), '后台残留会话', None, '', '', 0, 'gpt-6.1-sol', 'high', 0, 0, None),
            )
        with contextlib.closing(sqlite3.connect(codex_dir / 'thread_history_1.sqlite')) as tconn, tconn:
            tconn.execute(
                'INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',
                ('tid-bg-stop', 't-bg', 1, 'inProgress', 100, None),
            )

        # Without a writer lock, a stale inProgress turn must NOT block Rove.
        st_unlocked = self.client.get('/api/state').json()
        s_unlocked = next(x for x in st_unlocked['sessions'] if x.get('native_id') == 'tid-bg-stop')
        self.assertFalse(s_unlocked['external_active'])

        # With an active writer lock, external_active=True until user clicks Stop in Rove.
        lock_file = codex_dir / 'thread-writer-locks/tid-bg-stop.lock'
        lock_file.write_text('', encoding='utf-8')
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st_locked = self.client.get('/api/state').json()
            s_locked = next(x for x in st_locked['sessions'] if x['id'] == s_unlocked['id'])
            self.assertTrue(s_locked['external_active'])

            stop_res = self.post(f"sessions/{s_unlocked['id']}/stop")
            self.assertEqual(stop_res.status_code, 200)

            st_after_stop = self.client.get('/api/state').json()
            s_after_stop = next(x for x in st_after_stop['sessions'] if x['id'] == s_unlocked['id'])
            self.assertFalse(s_after_stop['external_active'])

            fake_bin = Path(self.temp.name) / 'fake-codex'
            fake_bin.write_text('#!/bin/sh\n', encoding='utf-8')
            cfg = db.config()
            cfg['codex_bin'] = str(fake_bin)
            db.write_private(db.DATA / 'config.json', json.dumps(cfg))

            with patch('workstation.server.spawn'):
                msg_res = self.post(f"sessions/{s_unlocked['id']}/messages", {'text': '停止后继续', 'request_id': secrets.token_hex(16)})
            self.assertEqual(msg_res.status_code, 200, msg_res.text)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_file_download_preserves_media_type_and_allows_session_cwd(self):
        extra_cwd = Path(self.temp.name) / 'external_cwd'
        extra_cwd.mkdir(parents=True)
        video_file = extra_cwd / 'ROVE-测试视频.mp4'
        video_file.write_bytes(b'\x00\x00\x00\x18ftypmp42')

        sess = self.post('sessions', {'project_id': 'project'}).json()
        db.execute('UPDATE sessions SET cwd=? WHERE id=?', (str(extra_cwd), sess['id']))

        meta_res = self.client.get(
            '/api/files/meta',
            params={'path': 'ROVE-测试视频.mp4', 'session_id': sess['id']},
        )
        self.assertEqual(meta_res.status_code, 200, meta_res.text)
        self.assertEqual(meta_res.json()['kind'], 'video')

        dl_res = self.client.get(
            '/api/files',
            params={'path': str(video_file), 'session_id': sess['id'], 'download': '1'},
        )
        self.assertEqual(dl_res.status_code, 200, dl_res.text)
        self.assertEqual(dl_res.headers.get('content-type'), 'video/mp4')
        self.assertIn('attachment;', dl_res.headers.get('content-disposition', ''))
        self.assertIn("filename*=UTF-8''ROVE-%E6%B5%8B%E8%AF%95%E8%A7%86%E9%A2%91.mp4", dl_res.headers.get('content-disposition', ''))


if __name__ == '__main__':
    unittest.main()
