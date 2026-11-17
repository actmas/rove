import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from workstation import agents, checkpoints, retention, scratch, server, store as db


class T3InspiredFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orig_data = db.DATA
        db.DATA = Path(self.temp.name) / 'data'
        self.project = Path(self.temp.name) / 'repo'
        self.project.mkdir(parents=True)
        subprocess.run(['git', 'init'], cwd=self.project, capture_output=True, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@local'], cwd=self.project, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.project, check=True)
        (self.project / 'app.py').write_text('print("v1")\n', encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=self.project, check=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=self.project, capture_output=True, check=True)

        db.init()
        cfg = db.config()
        cfg['allowed_roots'] = [str(self.project.parent)]
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))

        self.client = TestClient(server.app, base_url='http://localhost:8765')
        self.headers = {'Origin': 'http://localhost:8765', 'X-Workstation': '1'}
        pw = (db.DATA / 'access-password.txt').read_text().strip()
        r = self.client.post('/api/login', json={'password': pw}, headers=self.headers)
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        db.close_all_connections()
        db.DATA = self.orig_data
        self.temp.cleanup()

    def get(self, path):
        return self.client.get('/api/' + path, headers=self.headers)

    def post(self, path, body=None):
        return self.client.post('/api/' + path, json=body or {}, headers=self.headers)

    def test_1_hidden_git_checkpoint_turn_diff_and_revert(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()

        jid = secrets.token_hex(12)
        db.execute(
            "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'completed',?,'codex')",
            (jid, sess['id'], secrets.token_hex(8), 'update app', time.time()),
        )
        ref = checkpoints.capture_checkpoint(str(self.project), sess['id'], jid)
        self.assertTrue(ref.startswith('refs/rove/checkpoints/'))
        db.execute('UPDATE jobs SET checkpoint_ref=? WHERE id=?', (ref, jid))
        db.event(sess['id'], jid, 'user', {'text': 'update app'})
        db.event(sess['id'], jid, 'codex', {'method': 'item/completed', 'params': {'item': {'id': 'm1', 'type': 'agentMessage', 'text': 'updated'}}})

        # Simulate AI editing app.py and creating new_file.txt
        (self.project / 'app.py').write_text('print("v2")\n', encoding='utf-8')
        (self.project / 'new_file.txt').write_text('hello\n', encoding='utf-8')

        # Turn-scoped diff includes both tracked edit and untracked file
        diff_res = self.client.get(f"/api/sessions/{sess['id']}/jobs/{jid}/diff")
        self.assertEqual(diff_res.status_code, 200)
        ddata = diff_res.json()
        self.assertTrue(ddata['available'])
        self.assertIn('v2', ddata['diff'])
        self.assertIn('new_file.txt', ddata['diff'])

        # Revert restores app.py to v1, removes new_file.txt, and marks job reverted
        rev_res = self.post(f"sessions/{sess['id']}/jobs/{jid}/revert")
        self.assertEqual(rev_res.status_code, 200)
        self.assertEqual((self.project / 'app.py').read_text(encoding='utf-8'), 'print("v1")\n')
        self.assertFalse((self.project / 'new_file.txt').exists())
        self.assertEqual(db.one('SELECT reverted FROM jobs WHERE id=?', (jid,))['reverted'], 1)

    def test_2_zero_project_scratch_session_and_promote(self):
        sess = self.post('sessions', {'scratch': True, 'title': '快速脚本', 'engine': 'codex'}).json()
        proj = db.one('SELECT * FROM projects WHERE id=?', (sess['project_id'],))
        self.assertEqual(proj['is_scratch'], 1)
        self.assertTrue(Path(proj['path']).is_dir())
        self.assertFalse((Path(proj['path']) / '.git').exists())

        (Path(proj['path']) / 'helper.py').write_text('print(1)\n', encoding='utf-8')
        prom = self.post(f"sessions/{sess['id']}/promote", {'name': 'My Helper', 'init_git': True})
        self.assertEqual(prom.status_code, 200)
        pdata = prom.json()['project']
        self.assertEqual(pdata['is_scratch'], 0)
        self.assertEqual(pdata['name'], 'My Helper')
        self.assertTrue((Path(pdata['path']) / '.git').exists())

    def test_3_pruned_through_seq_watermark_and_git_commit(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()
        old_ts = time.time() - 30 * 86400
        db.execute('UPDATE sessions SET updated=? WHERE id=?', (old_ts, sess['id']))
        db.event(sess['id'], 'j1', 'codex', {'method': 'item/agentMessage/delta', 'params': {'delta': 'hi'}})
        max_id = db.one('SELECT MAX(id) AS m FROM events WHERE session_id=?', (sess['id'],))['m']
        db.event(sess['id'], 'j1', 'codex', {'method': 'item/completed', 'params': {'item': {'id': 'x', 'type': 'agentMessage', 'text': 'hi'}}})

        res = retention.compact_old_events(cutoff_timestamp=time.time() - 7 * 86400, keep_pinned=False)
        self.assertGreaterEqual(res['codex_deltas_deleted'], 1)
        updated_sess = db.one('SELECT pruned_through_seq FROM sessions WHERE id=?', (sess['id'],))
        self.assertGreaterEqual(updated_sess['pruned_through_seq'], max_id)

        # Git commit endpoint
        (self.project / 'app.py').write_text('print("committed")\n', encoding='utf-8')
        c_res = self.post(f"projects/{proj['id']}/git/commit", {'message': 'feat: update app'})
        self.assertEqual(c_res.status_code, 200)
        self.assertTrue(c_res.json()['ok'])

    def test_4_acp_agent_registration_and_stdio_execution(self):
        fake_acp = Path(self.temp.name) / 'fake_acp.py'
        fake_acp.write_text('''import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}}), flush=True)
    elif m == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "acp-sess-1"}}), flush=True)
    elif m == "session/prompt":
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "ACP_OK"}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}}), flush=True)
''', encoding='utf-8')
        wrapper = Path(self.temp.name) / 'fake-acp-bin'
        import sys as _sys
        wrapper.write_text(f'#!/bin/sh\nexec "{_sys.executable}" "{fake_acp}" "$@"\n', encoding='utf-8')
        wrapper.chmod(0o755)

        reg = self.post('agents', {
            'id': 'my-acp',
            'name': 'My ACP',
            'adapter': 'acp',
            'binary': str(wrapper),
            'args_template': [],
        })
        self.assertEqual(reg.status_code, 200, reg.text)

        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'my-acp'}).json()
        job_id = secrets.token_hex(12)
        db.execute(
            "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'running',?,'my-acp')",
            (job_id, sess['id'], secrets.token_hex(8), 'hello acp', time.time()),
        )
        job = db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        outcome = asyncio.run(agents.run_custom_agent('my-acp', job, sess, proj, []))
        self.assertEqual(outcome['status'], 'completed')
        evs = db.query('SELECT payload FROM events WHERE session_id=? AND job_id=?', (sess['id'], job_id))
        self.assertTrue(any('ACP_OK' in (ev.get('payload') or '') for ev in evs))

    def test_5_fixed_turn_diff_and_conflict_checked_revert(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()
        job1 = 'job1_' + secrets.token_hex(6)
        job2 = 'job2_' + secrets.token_hex(6)
        t1 = time.time() - 10
        t2 = time.time() - 5

        before1 = checkpoints.capture_checkpoint(str(self.project), sess['id'], job1, phase='before')
        (self.project / 'main.py').write_text("print('turn1')\n", encoding='utf-8')
        after1 = checkpoints.capture_checkpoint(str(self.project), sess['id'], job1, phase='after')
        db.execute(
            'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,checkpoint_ref,checkpoint_after_ref) VALUES(?,?,?,?,?,?,?,?)',
            (job1, sess['id'], secrets.token_hex(8), 'turn 1', 'completed', t1, before1, after1),
        )

        # Turn 2 modifies another file; turn 1's diff must remain scoped to main.py only!
        before2 = checkpoints.capture_checkpoint(str(self.project), sess['id'], job2, phase='before')
        (self.project / 'later.txt').write_text('later change\n', encoding='utf-8')
        after2 = checkpoints.capture_checkpoint(str(self.project), sess['id'], job2, phase='after')
        db.execute(
            'INSERT INTO jobs(id,session_id,request_id,prompt,status,started,checkpoint_ref,checkpoint_after_ref) VALUES(?,?,?,?,?,?,?,?)',
            (job2, sess['id'], secrets.token_hex(8), 'turn 2', 'completed', t2, before2, after2),
        )

        d1 = self.get(f"sessions/{sess['id']}/jobs/{job1}/diff").json()
        self.assertTrue(d1['available'])
        self.assertTrue(d1['scoped'])
        self.assertIn('turn1', d1['diff'])
        self.assertNotIn('later change', d1['diff'])

        # Reverting job1 without force should warn about later job2
        r_warn = self.post(f"sessions/{sess['id']}/jobs/{job1}/revert", {}).json()
        self.assertFalse(r_warn['ok'])
        self.assertTrue(r_warn['requires_confirm'])
        self.assertEqual(r_warn['later_jobs_count'], 1)
        self.assertTrue((self.project / 'later.txt').exists())

        # Force revert restores pre-turn-1 state
        r_force = self.post(f"sessions/{sess['id']}/jobs/{job1}/revert", {'force': True}).json()
        self.assertTrue(r_force['ok'])
        self.assertFalse((self.project / 'later.txt').exists())

    def test_6_queue_pause_handoff_preview_and_diagnostics(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()

        q_add = self.post(f"sessions/{sess['id']}/queue", {'text': 'queued task 1'}).json()
        self.assertEqual(q_add['prompt'], 'queued task 1')

        p_res = self.post(f"sessions/{sess['id']}/queue/pause", {'paused': True}).json()
        self.assertTrue(p_res['paused'])

        q_list = self.get(f"sessions/{sess['id']}/queue").json()
        self.assertTrue(q_list['paused'])
        self.assertFalse(q_list['supports_live_steer'])
        self.assertEqual(len(q_list['queue']), 1)

        # Add 6 turns so handoff-preview has omitted history
        for idx in range(6):
            jid = f'hj_{idx}_' + secrets.token_hex(4)
            ts = time.time() + idx
            db.execute(
                "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'completed',?,'codex')",
                (jid, sess['id'], secrets.token_hex(8), f'turn {idx} prompt', ts),
            )
            db.event(sess['id'], jid, 'user', {'text': f'turn {idx} prompt'})
            db.event(sess['id'], jid, 'assistant_delta', {'text': f'turn {idx} reply'})

        hp = self.get(f"sessions/{sess['id']}/handoff-preview?target_engine=antigravity").json()
        self.assertTrue(hp['needed'])
        self.assertEqual(hp['total_turns'], 6)
        self.assertEqual(hp['included_turns'], 4)
        self.assertEqual(hp['omitted_count'], 2)

        diag = self.get('diagnostics').json()
        self.assertTrue(diag['ok'])
        self.assertIn('engines', diag)


if __name__ == '__main__':
    unittest.main()
