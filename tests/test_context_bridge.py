import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

tmp_data = Path(tempfile.mkdtemp(prefix='workstation-bridge-test-'))
os.environ['WORKSTATION_DATA'] = str(tmp_data)

from workstation import store as db
from workstation import context_bridge
from workstation.server import app


class ContextBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data = db.DATA
        cls.tmp_data = Path(tempfile.mkdtemp(prefix='workstation-bridge-test-'))
        db.DATA = cls.tmp_data
        db.init()
        cls.client = TestClient(app, base_url='http://localhost:8765')
        cls.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        plain = (db.DATA / 'access-password.txt').read_text().strip()
        login_res = cls.client.post('/api/login', json={'password': plain}, headers=cls.headers)
        assert login_res.status_code == 200, login_res.text
        cls.cookie = login_res.cookies['walking_session']

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        db.DATA = cls._orig_data
        shutil.rmtree(cls.tmp_data, ignore_errors=True)
        shutil.rmtree(tmp_data, ignore_errors=True)

    def auth_headers(self):
        return dict(self.headers)

    def test_extract_turns_and_bridge_handoff(self):
        projects = db.query('SELECT * FROM projects')
        self.assertTrue(len(projects) > 0)
        proj = projects[0]

        # 1. Create a session starting with Codex
        sid = 'bridge-session-' + os.urandom(6).hex()
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, last_engine) VALUES(?,?,?,?,?,?,?)',
            (sid, proj['id'], 'codex', '跨模型测试会话', now, now, 'codex'),
        )
        session = db.one('SELECT * FROM sessions WHERE id=?', (sid,))

        # 2. Before any turns, should_bridge_context is False
        self.assertFalse(context_bridge.should_bridge_context(session, 'codex'))
        self.assertFalse(context_bridge.should_bridge_context(session, 'antigravity'))

        # 3. Simulate Turn 1: User asks, Codex modifies file and responds
        job_1 = 'job-c1-' + os.urandom(4).hex()
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, job_1, 'user', json.dumps({'text': '请设计并实现用户身份系统', 'engine': 'codex'}), now),
        )
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, job_1, 'codex', json.dumps({
                'method': 'item/completed',
                'params': {
                    'item': {
                        'type': 'fileChange',
                        'changes': [{'path': 'src/auth.py', 'kind': {'type': 'add'}}],
                    }
                }
            }), now + 1),
        )
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, job_1, 'codex', json.dumps({
                'method': 'item/completed',
                'params': {
                    'item': {
                        'type': 'agentMessage',
                        'text': '已成功创建 src/auth.py，实现了 JWT 认证与密码哈希处理。',
                    }
                }
            }), now + 2),
        )

        # Verify extracted turns
        turns = context_bridge.extract_session_turns(sid)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['engine'], 'codex')
        self.assertEqual(turns[0]['prompt'], '请设计并实现用户身份系统')
        self.assertIn('src/auth.py', turns[0]['files'])
        self.assertIn('已成功创建 src/auth.py', turns[0]['reply'])

        # 4. Same engine (Codex) with native_codex_id set -> no handoff needed
        db.execute('UPDATE sessions SET native_codex_id=? WHERE id=?', ('codex-tid-1', sid))
        session = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        self.assertFalse(context_bridge.should_bridge_context(session, 'codex'))

        # 5. Switching to Antigravity -> handoff needed!
        self.assertTrue(context_bridge.should_bridge_context(session, 'antigravity'))

        # 6. Build handoff context (even when current turn's user event is already inserted!)
        job_2 = 'job-a2-' + os.urandom(4).hex()
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, job_2, 'user', json.dumps({'text': '继续完善权限控制', 'engine': 'antigravity'}), now + 5),
        )
        handoff = context_bridge.build_handoff_context(
            session, proj, target_engine='antigravity', exclude_job_id=job_2
        )
        self.assertIn('`CODEX` ➔ `ANTIGRAVITY`', handoff)
        self.assertIn('请设计并实现用户身份系统', handoff)
        self.assertIn('src/auth.py', handoff)
        self.assertIn('JWT 认证与密码哈希处理', handoff)
        self.assertIn('接力执行要求', handoff)

    def test_antigravity_to_codex_switch_never_leaks_native_id(self):
        projects = db.query('SELECT * FROM projects')
        proj = projects[0]
        sid = 'agy_' + os.urandom(6).hex()
        agy_cid = '14c535a4-46fb-41c4-8b63-8736ea62292c'
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, last_engine, title, native_id, native_antigravity_id, created, updated) VALUES(?,?,?,?,?,?,?,?,?)',
            (sid, proj['id'], 'antigravity', 'antigravity', 'AGY会话', agy_cid, agy_cid, now, now),
        )
        # Simulate synced Antigravity events (job_id is NULL)
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,NULL,?,?,?)',
            (sid, 'user', json.dumps({'text': '以方案1进行优化', 'source': 'antigravity_sync'}), now),
        )
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,NULL,?,?,?)',
            (sid, 'antigravity', json.dumps({
                'event': 'agy_step',
                'step_kind': 'agent',
                'text': '已完成方案1跨引擎上下文桥接器实现。',
                'completed': True,
            }), now + 1),
        )

        # Switch session to codex via PATCH
        patch_res = self.client.patch(
            f'/api/sessions/{sid}',
            json={'engine': 'codex', 'model': 'gpt-5.3-codex'},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(patch_res.status_code, 200)
        updated_s = patch_res.json()
        self.assertEqual(updated_s['engine'], 'codex')
        # Critical invariant: Codex native ID must be None, never the Antigravity UUID!
        self.assertIsNone(db.get_codex_native_id(updated_s))
        self.assertEqual(db.get_antigravity_native_id(updated_s), agy_cid)

        # Simulate current Codex job user event already inserted before worker runs
        curr_job = 'job-codex-new'
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, curr_job, 'user', json.dumps({'text': '分析总结一下这个项目', 'engine': 'codex'}), now + 2),
        )
        handoff = context_bridge.build_handoff_context(
            updated_s, proj, target_engine='codex', exclude_job_id=curr_job
        )
        self.assertIn('`ANTIGRAVITY` ➔ `CODEX`', handoff)
        self.assertIn('以方案1进行优化', handoff)
        self.assertIn('已完成方案1跨引擎上下文桥接器实现', handoff)

    def test_session_engine_switching_api(self):
        projects = db.query('SELECT * FROM projects')
        proj = projects[0]

        # Create session via API
        res = self.client.post(
            '/api/sessions',
            json={'project_id': proj['id'], 'engine': 'codex', 'title': '切换测试'},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(res.status_code, 200)
        s_data = res.json()
        sid = s_data['id']
        self.assertEqual(s_data['engine'], 'codex')

        # Switch engine to antigravity via PATCH /api/sessions/{id}
        patch_res = self.client.patch(
            f'/api/sessions/{sid}',
            json={'engine': 'antigravity', 'model': 'gemini-2.5-pro'},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(patch_res.status_code, 200)
        updated_s = patch_res.json()
        self.assertEqual(updated_s['engine'], 'antigravity')

    def test_incremental_delta_handoff_and_reply_compression(self):
        projects = db.query('SELECT * FROM projects')
        proj = projects[0]
        sid = 'delta-session-' + os.urandom(6).hex()
        now = time.time()
        # Both engines already have native session IDs -> incremental delta handoff mode
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, last_engine, title, native_codex_id, native_antigravity_id, created, updated) VALUES(?,?,?,?,?,?,?,?,?)',
            (sid, proj['id'], 'antigravity', 'codex', '增量接力测试', 'codex-thread-1', 'agy-conv-1', now, now),
        )
        # Turn 1: Antigravity turn (already in agy-conv-1's native memory)
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, 'job-1', 'user', json.dumps({'text': '第一轮AGY早期提问', 'engine': 'antigravity'}), now),
        )
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, 'job-1', 'antigravity', json.dumps({
                'event': 'agy_step', 'step_kind': 'agent', 'text': '第一轮AGY早期回答', 'completed': True
            }), now + 1),
        )
        # Turn 2: Codex turn with a long code block
        long_code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(15)) + "\n```"
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, 'job-2', 'user', json.dumps({'text': '第二轮Codex增量提问', 'engine': 'codex'}), now + 2),
        )
        db.execute(
            'INSERT INTO events(session_id, job_id, kind, payload, created) VALUES(?,?,?,?,?)',
            (sid, 'job-2', 'codex', json.dumps({
                'method': 'item/completed',
                'params': {'item': {'type': 'agentMessage', 'text': f'已完成修改：\n{long_code}\n请验证。'}}
            }), now + 3),
        )

        session = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        handoff = context_bridge.build_handoff_context(session, proj, target_engine='antigravity')
        # Should use delta header and exclude Turn 1 (which Antigravity already knows)
        self.assertIn('增量差量交接', handoff)
        self.assertNotIn('第一轮AGY早期提问', handoff)
        self.assertIn('第二轮Codex增量提问', handoff)
        # Multi-line code block should be compressed to save tokens
        self.assertIn('[代码详见对应本地文件]', handoff)
        self.assertNotIn('line_10 = 10', handoff)
    def test_delete_relay_session_dismisses_both_native_threads(self):
        proj = db.query('SELECT * FROM projects')[0]
        sid = 'relay_del_' + os.urandom(5).hex()
        cid, aid = 'codex-' + os.urandom(4).hex(), 'agy-' + os.urandom(4).hex()
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, last_engine, title, native_id, native_codex_id, native_antigravity_id, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (sid, proj['id'], 'codex', 'codex', '接力删除', cid, cid, aid, now, now),
        )
        res = self.client.delete(
            f'/api/sessions/{sid}',
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(db.one('SELECT 1 AS x FROM dismissed_codex_threads WHERE native_id=?', (cid,)))
        self.assertTrue(db.one('SELECT 1 AS x FROM dismissed_antigravity_threads WHERE native_id=?', (aid,)))

    def test_webpush_module_has_runtime_dependencies(self):
        from workstation import webpush
        self.assertTrue(hasattr(webpush, 'asyncio'))
        self.assertTrue(hasattr(webpush, 'contextlib'))

    def test_foreign_engine_model_is_never_used(self):
        from workstation import engines
        codex_default = next(m for m in engines.engine_models('codex')['models'] if m.get('isDefault'))['id']
        agy_ids = {m['id'] for m in engines.engine_models('antigravity')['models']}
        model, _ = engines.resolve_model('antigravity', codex_default, 'low')
        self.assertIn(model, agy_ids)
        self.assertNotEqual(model, codex_default)
        # Own-engine and custom ids are preserved
        own = next(iter(agy_ids))
        self.assertEqual(engines.resolve_model('antigravity', own, None)[0], own)
        self.assertEqual(engines.resolve_model('antigravity', 'my-custom-model', None)[0], 'my-custom-model')

        proj = db.query('SELECT * FROM projects')[0]
        sid = 'foreign_' + os.urandom(5).hex()
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, last_engine, title, model, created, updated) VALUES(?,?,?,?,?,?,?,?)',
            (sid, proj['id'], 'codex', 'codex', '模型串用', codex_default, now, now),
        )
        res = self.client.patch(
            f'/api/sessions/{sid}',
            json={'engine': 'antigravity'},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(res.json()['model'], agy_ids)


if __name__ == '__main__':
    unittest.main()
