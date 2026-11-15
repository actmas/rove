import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class CloudExecutionTests(unittest.TestCase):
    def setUp(self):
        global db, agents, cloud_execution, app
        from workstation import store as db, agents, cloud_execution
        from workstation.server import app
        self.old = db.DATA
        self.tmp = tempfile.TemporaryDirectory()
        db.DATA = Path(self.tmp.name) / 'data'
        db.init()
        self.client = TestClient(app, base_url='http://localhost:8765')
        self.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        self.client.post('/api/login', json={'password': (db.DATA/'access-password.txt').read_text().strip()}, headers=self.headers)
        self.project = db.query('SELECT * FROM projects')[0]
        self.sid = self.client.post('/api/sessions', json={'project_id':self.project['id'], 'engine':'codex'}, headers=self.headers).json()['id']

    def tearDown(self):
        self.client.close()
        db.DATA = self.old
        self.tmp.cleanup()

    def test_capability_and_custom_cloud_url(self):
        specs = {a['id']:a for a in agents.list_all_agents()}
        self.assertEqual(specs['codex']['execution_targets'], ['local','cloud'])
        self.assertEqual(specs['antigravity']['execution_targets'], ['local'])
        r = self.client.post('/api/agents', json={'id':'test-agent','binary':'echo','adapter':'cli', 'cloud_url':'https://example.com/agent'}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(agents.get_agent_spec('test-agent')['cloud_url'], 'https://example.com/agent')
        self.assertEqual(next(a for a in agents.list_all_agents() if a['id']=='test-agent')['execution_targets'], ['local','cloud'])

    def test_dispatch_and_thread_isolation(self):
        db.execute('UPDATE sessions SET native_codex_id=? WHERE id=?', ('local-thread',self.sid))
        body = {'text':'检查项目','request_id':'cloud-test-123456789','execution_target':'cloud','cloud_environment':'env_test'}
        with patch('workstation.server.spawn'), patch('workstation.agents.is_agent_installed', return_value=True), patch('workstation.codex_sync.sync_session_events', return_value=0), patch('workstation.codex_sync.is_thread_locked', return_value=True):
            res = self.client.post(f'/api/sessions/{self.sid}/messages', json=body, headers=self.headers)
        self.assertEqual(res.status_code,200,res.text)
        job=res.json()
        self.assertEqual(job['execution_target'],'cloud')
        self.assertIsNone(job['model'])
        session=db.one('SELECT * FROM sessions WHERE id=?',(self.sid,))
        with patch.object(cloud_execution,'command', new=AsyncMock(return_value='https://chatgpt.com/codex/tasks/task_abc123')) as run:
            result=asyncio.run(cloud_execution.submit(job,session,self.project))
        self.assertEqual(result['status'],'submitted')
        self.assertIn('--env',run.call_args.args)
        self.assertEqual(db.one('SELECT native_codex_id FROM sessions WHERE id=?',(self.sid,))['native_codex_id'],'local-thread')
        self.assertEqual(db.one('SELECT cloud_task_id FROM jobs WHERE id=?',(job['id'],))['cloud_task_id'],'task_abc123')
        with patch.object(cloud_execution, 'command', new=AsyncMock(side_effect=['Status: ready','diff --git a/x b/x'])):
            data=self.client.get(f'/api/jobs/{job["id"]}/cloud').json()
        self.assertIn('ready',data['status'])
        self.assertIn('diff --git',data['diff'])

    def test_reject_unconfigured_cloud_and_invalid_target(self):
        for extra in [{'execution_target':'cloud'}, {'execution_target':'elsewhere'}, {'engine':'antigravity','execution_target':'cloud','cloud_environment':'env_test'}]:
            body={'text':'检查','request_id':'invalid-cloud-123456789',**extra}
            response=self.client.post(f'/api/sessions/{self.sid}/messages',json=body,headers=self.headers)
            self.assertIn(response.status_code,(400,422))
        self.assertEqual(db.one('SELECT count(*) AS n FROM jobs')['n'],0)

    def test_unrecognized_submission_does_not_claim_completion(self):
        job={'id':'none','prompt':'检查','cloud_environment':'env_test'}
        db.execute('INSERT INTO jobs(id,session_id,request_id,prompt,status) VALUES(?,?,?,?,?)', ('none',self.sid,'unrecognized-request','检查','queued'))
        session=db.one('SELECT * FROM sessions WHERE id=?',(self.sid,))
        async def go():
            with patch.object(cloud_execution, 'command', new=AsyncMock(return_value='Unknown output')):
                with self.assertRaisesRegex(RuntimeError,'未识别'):
                    await cloud_execution.submit(job,session,self.project)
        asyncio.run(go())

    def test_cloud_task_event_and_antigravity_cloud_toggle(self):
        job={'id':'job-cloud-evt','prompt':'测试云端','cloud_environment':'env_my_prod'}
        db.execute('INSERT INTO jobs(id,session_id,request_id,prompt,status) VALUES(?,?,?,?,?)',
                   ('job-cloud-evt',self.sid,'req-cloud-evt','测试云端','queued'))
        session=db.one('SELECT * FROM sessions WHERE id=?',(self.sid,))
        with patch.object(cloud_execution, 'command', new=AsyncMock(return_value='Task created: task_xyz789')):
            res = asyncio.run(cloud_execution.submit(job, session, self.project))
        self.assertEqual(res['status'], 'submitted')
        evts = db.query('SELECT * FROM events WHERE session_id=? AND job_id=?', (self.sid, 'job-cloud-evt'))
        cloud_evt = next((e for e in evts if e['kind'] == 'cloud_task'), None)
        self.assertIsNotNone(cloud_evt)
        payload = json.loads(cloud_evt['payload'])
        self.assertEqual(payload['task_id'], 'task_xyz789')
        self.assertEqual(payload['environment'], 'env_my_prod')

        # Test antigravity cloud toggle when antigravity_cloud_url is configured in config
        with patch('workstation.store.config', return_value={'antigravity_cloud_url': 'https://idx.google.com'}):
            spec = agents.get_agent_spec('antigravity')
            self.assertEqual(spec['execution_targets'], ['local', 'cloud'])
            self.assertEqual(spec['cloud_url'], 'https://idx.google.com')

    def test_snapshot_pages_and_active_deltas(self):
        for i in range(25):
            db.event(self.sid, None, 'user', {'text': str(i)})
        db.event(self.sid, None, 'codex', {'method': 'item/agentMessage/delta', 'params': {'itemId': 'finished', 'delta': '旧'}})
        db.event(self.sid, None, 'codex', {'method': 'item/completed', 'params': {'item': {'id': 'finished', 'type': 'agentMessage', 'text': '最终'}}})
        db.event(self.sid, None, 'codex', {'method': 'item/agentMessage/delta', 'params': {'itemId': 'active', 'delta': '进行中'}})
        page = self.client.get(f'/api/sessions/{self.sid}/snapshot').json()
        self.assertTrue(page['has_older'])
        self.assertEqual(sum(e['kind'] == 'user' for e in page['events']), 20)
        deltas = [e['payload']['params']['itemId'] for e in page['events'] if e['payload'].get('method') == 'item/agentMessage/delta']
        self.assertEqual(deltas, ['active'])
        older = self.client.get(f'/api/sessions/{self.sid}/snapshot?before={page["oldest"]}').json()
        self.assertEqual(len(older['events']), 5)
        self.assertFalse(older['has_older'])
        self.assertLess(older['cursor'], page['oldest'])

    def test_boot_watchdog_and_watch_assets_edge_cache(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('preboot-fallback', page.text)
        self.assertNotIn('/healthz?lite=1', page.text)
        self.assertIn("script-src 'self' 'unsafe-inline'", page.headers['content-security-policy'])
        self.assertIn('/watch/assets/', page.text)

