from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from starlette.testclient import TestClient

from workstation import store as db
from workstation import retention
from workstation.server import app, COOKIE


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_data = db.DATA
        db.DATA = Path(self.tmp)
        db.init()
        # Seed an admin login
        self.token = 'test-admin-secret-token'
        db.execute(
            'INSERT INTO logins VALUES(?,?)',
            (db.hashlib.sha256(self.token.encode()).hexdigest(), time.time() + 3600),
        )
        self.client = TestClient(app, base_url='http://localhost:8765')
        self.client.cookies.set(COOKIE, self.token)
        self.headers = {'Origin': 'http://localhost:8765', 'X-Workstation': '1'}

    def tearDown(self):
        db.DATA = self.orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_storage_stats(self):
        stats = retention.get_storage_stats()
        self.assertIn('total_size_bytes', stats)
        self.assertIn('tables', stats)
        self.assertIn('events', stats['tables'])
        self.assertIn('diagnostics', stats)
        self.assertGreaterEqual(stats['diagnostics']['total_sessions'], 0)

    def test_compact_events_safety_invariants(self):
        proj = db.one('SELECT id FROM projects LIMIT 1')
        proj_id = proj['id'] if proj else 'p1'

        now = time.time()
        # Create 2 sessions: s_active (has running job), s_finished (all completed)
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_active', proj_id, 'codex', 'Active Session', now, now, 0),
        )
        db.execute(
            'INSERT INTO jobs(id, session_id, request_id, prompt, status, started) VALUES(?,?,?,?,?,?)',
            ('j_active', 's_active', 'req_act', 'Active Job', 'running', now),
        )

        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_finished', proj_id, 'codex', 'Finished Session', now - 86400, now - 86400, 0),
        )
        db.execute(
            'INSERT INTO jobs(id, session_id, request_id, prompt, status, started, ended) VALUES(?,?,?,?,?,?,?)',
            ('j_finished', 's_finished', 'req_fin', 'Finished Job', 'completed', now - 86400, now - 86300),
        )

        # Insert events for s_active: deltas must be untouched
        db.event('s_active', 'j_active', 'user', {'text': 'Active user prompt'})
        db.event('s_active', 'j_active', 'codex', {
            'method': 'item/agentMessage/delta',
            'params': {'itemId': 'msg1', 'delta': 'streaming token'},
        })

        # Insert events for s_finished:
        # 1. user prompt (MUST BE PRESERVED)
        db.event('s_finished', 'j_finished', 'user', {'text': 'Preserve this question'})
        # 2. streaming deltas (SHOULD BE COMPACTED)
        db.event('s_finished', 'j_finished', 'codex', {
            'method': 'item/agentMessage/delta',
            'params': {'itemId': 'msg2', 'delta': 'delta token'},
        })
        db.event('s_finished', 'j_finished', 'codex', {
            'method': 'item/commandExecution/outputDelta',
            'params': {'itemId': 'cmd1', 'delta': 'stdout chunk'},
        })
        # 3. completed item (MUST BE PRESERVED)
        db.event('s_finished', 'j_finished', 'codex', {
            'method': 'item/completed',
            'params': {'item': {'type': 'agentMessage', 'text': 'Final completed response'}},
        })
        # 4. bloated base64 image (SHOULD BE COMPACTED)
        saved_image = Path(self.tmp) / 'img.png'
        saved_image.write_bytes(b'image')
        huge_base64 = 'data:image/png;base64,' + ('A' * 12000)
        db.event('s_finished', 'j_finished', 'codex', {
            'method': 'item/completed',
            'params': {
                'item': {
                    'type': 'imageGeneration',
                    'result': huge_base64,
                    'savedPath': str(saved_image),
                }
            },
        })

        db.event('s_finished', 'j_finished', 'codex', {
            'method': 'item/completed',
            'params': {'item': {'type': 'imageGeneration', 'id': 'no-backup', 'result': huge_base64}},
        })

        # Run compaction
        res = retention.compact_old_events(cutoff_timestamp=None, dry_run=False, keep_pinned=True)
        self.assertEqual(res['codex_deltas_deleted'], 2)
        self.assertEqual(res['images_compacted'], 1)

        # Verify active session events were not touched
        active_deltas = db.query("SELECT * FROM events WHERE session_id='s_active' AND kind='codex'")
        self.assertEqual(len(active_deltas), 1)

        # Verify finished session user prompt and completed item are preserved
        user_ev = db.one("SELECT payload FROM events WHERE session_id='s_finished' AND kind='user'")
        self.assertIn('Preserve this question', user_ev['payload'])

        completed_ev = db.one(
            "SELECT payload FROM events WHERE session_id='s_finished' AND json_extract(payload, '$.params.item.type')='agentMessage'"
        )
        self.assertIn('Final completed response', completed_ev['payload'])

        # Verify image base64 was compacted
        compacted_img_ev = db.one(
            "SELECT payload FROM events WHERE session_id='s_finished' AND json_extract(payload, '$.params.item.type')='imageGeneration'"
        )
        self.assertNotIn('data:image/png;base64,', compacted_img_ev['payload'])
        self.assertIn('[embedded base64 archived; see savedPath]', compacted_img_ev['payload'])
        preserved = db.one("SELECT payload FROM events WHERE json_extract(payload, '$.params.item.id')='no-backup'")
        self.assertEqual(json.loads(preserved['payload'])['params']['item']['result'], huge_base64)

    def test_prune_sessions_and_foreign_keys(self):
        proj = db.one('SELECT id FROM projects LIMIT 1')
        proj_id = proj['id'] if proj else 'p1'

        now = time.time()
        cutoff_7d = now - 7 * 86400

        # Old unpinned session (should be pruned)
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_old', proj_id, 'codex', 'Old Session', cutoff_7d - 100, cutoff_7d - 100, 0),
        )
        db.execute(
            'INSERT INTO jobs(id, session_id, request_id, prompt, status, started, ended) VALUES(?,?,?,?,?,?,?)',
            ('j_old', 's_old', 'req_old', 'Old Job', 'completed', cutoff_7d - 100, cutoff_7d - 50),
        )
        db.execute('INSERT INTO terminals(id, session_id, status, pid, created) VALUES(?,?,?,?,?)', ('t_old', 's_old', 'exited', 9999, cutoff_7d - 100))
        db.execute('INSERT INTO terminal_output(terminal_id, data) VALUES(?,?)', ('t_old', 'output'))
        db.event('s_old', 'j_old', 'user', {'text': 'Old prompt'})

        # Old PINNED session (must NOT be pruned)
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_pinned', proj_id, 'codex', 'Pinned Session', cutoff_7d - 100, cutoff_7d - 100, 1),
        )

        res = retention.prune_sessions(cutoff_timestamp=cutoff_7d, dry_run=False, keep_pinned=True)
        self.assertEqual(res['pruned_sessions'], 1)
        self.assertEqual(res['pruned_events'], 1)

        # Check that s_old is gone
        self.assertIsNone(db.one("SELECT id FROM sessions WHERE id='s_old'"))
        # Check that s_pinned remains
        self.assertIsNotNone(db.one("SELECT id FROM sessions WHERE id='s_pinned'"))

    def test_ephemeral_pruning_and_vacuum(self):
        proj = db.one('SELECT id FROM projects LIMIT 1')
        proj_id = proj['id'] if proj else 'p1'
        now = time.time()
        # Add expired login
        db.execute('INSERT INTO logins VALUES(?,?)', ('expired_hash', now - 100))
        # Add a session and an old exited terminal output
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_eph', proj_id, 'codex', 'Ephemeral Test', now, now, 0),
        )
        db.execute('INSERT INTO terminals(id, session_id, status, pid, created) VALUES(?,?,?,?,?)', ('t_temp', 's_eph', 'exited', 999, now - 8*86400))
        db.execute('INSERT INTO terminal_output(terminal_id, data) VALUES(?,?)', ('t_temp', 'ancient chunk'))

        eph = retention.prune_ephemeral(dry_run=False)
        self.assertGreaterEqual(eph['expired_logins'], 1)
        self.assertGreaterEqual(eph['old_terminal_outputs'], 1)

        # Test vacuum
        vac = retention.vacuum_database()
        self.assertIn('freed_bytes', vac)
        self.assertIn('size_after_formatted', vac)

    def test_storage_api_endpoints(self):
        # 1. GET /api/system/storage
        res = self.client.get('/api/system/storage')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertIn('total_size_formatted', data)
        self.assertIn('diagnostics', data)

        # 2. POST /api/system/storage/prune (dry-run)
        body = {
            'keep_days': 14,
            'compact_events': True,
            'compact_all': True,
            'prune_sessions': False,
            'vacuum': True,
            'backup_before': False,
            'dry_run': True,
        }
        res2 = self.client.post('/api/system/storage/prune', json=body, headers=self.headers)
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2['ok'])
        self.assertTrue(data2['dry_run'])
        self.assertIn('stats', data2)

    def test_terminal_output_ring_buffer_pruning(self):
        now = time.time()
        proj = db.one('SELECT id FROM projects LIMIT 1')
        proj_id = proj['id'] if proj else 'p1'
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, pinned) VALUES(?,?,?,?,?,?,?)',
            ('s_term_ring', proj_id, 'codex', 'Terminal Ring Buffer Test', now, now, 0),
        )
        term_id = 't_ring_test'
        db.execute('INSERT INTO terminals(id, session_id, status, pid, created) VALUES(?,?,?,?,?)', (term_id, 's_term_ring', 'running', 1234, now))

        # Insert 1600 output chunks
        for i in range(1600):
            db.execute('INSERT INTO terminal_output(terminal_id, data) VALUES(?,?)', (term_id, f'chunk {i}'))

        initial_count = db.one('SELECT count(*) as cnt FROM terminal_output WHERE terminal_id=?', (term_id,))['cnt']
        self.assertEqual(initial_count, 1600)

        # Trigger pruning logic as used in terminal.py
        cutoff = db.one('SELECT id FROM terminal_output WHERE terminal_id=? ORDER BY id DESC LIMIT 1 OFFSET 1500', (term_id,))
        self.assertIsNotNone(cutoff)
        db.execute('DELETE FROM terminal_output WHERE terminal_id=? AND id <= ?', (term_id, cutoff['id']))

        pruned_count = db.one('SELECT count(*) as cnt FROM terminal_output WHERE terminal_id=?', (term_id,))['cnt']
        self.assertEqual(pruned_count, 1500)


if __name__ == '__main__':
    unittest.main()
