import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from workstation import store as db
from workstation import cluster_sync, feishu_relay


class ApprovalAndSyncRegressions(unittest.TestCase):
    def setUp(self):
        self.original_data = db.DATA
        self.temp = tempfile.TemporaryDirectory()
        db.DATA = Path(self.temp.name) / 'data'
        db.init()
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        config = db.config().copy()
        config['allowed_roots'] = [str(self.project)]
        db.write_private(db.DATA / 'config.json', json.dumps(config))
        db.execute('INSERT INTO projects(id,name,path) VALUES(?,?,?)', ('p', 'test', str(self.project)))
        for ident in ('a', 'b'):
            db.execute(
                'INSERT INTO sessions(id,project_id,engine,title,created,updated) VALUES(?,?,?,?,?,?)',
                (ident, 'p', 'codex', ident, time.time(), time.time()),
            )
            db.execute(
                'INSERT INTO jobs(id,session_id,request_id,prompt,status,started) VALUES(?,?,?,?,?,?)',
                (ident, ident, ident, 'test', 'waiting', time.time()),
            )
            db.execute(
                'INSERT INTO approvals(id,job_id,method,payload,created) VALUES(?,?,?,?,?)',
                (ident, ident, 'item/commandExecution/requestApproval', '{}', time.time()),
            )

    def tearDown(self):
        conn = getattr(db._local, 'conn', None)
        if conn is not None:
            conn.close()
        db.DATA = self.original_data
        self.temp.cleanup()

    def test_legacy_activity_data_is_removed_on_upgrade(self):
        db.execute('CREATE TABLE live_activity_tokens(id TEXT PRIMARY KEY, token TEXT)')
        db.execute('INSERT INTO live_activity_tokens VALUES(?,?)', ('old', 'old-token'))
        db.init()
        self.assertIsNone(db.one("SELECT name FROM sqlite_master WHERE name='live_activity_tokens'"))

    def test_concurrent_decisions_have_one_winner(self):
        def decide(value):
            return db.decide_approval('a', value)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ['accept', 'decline']))
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse(db.decide_approval('a', 'cancel'))

    def test_channel_delivery_cannot_decide_two_approvals(self):
        self.assertTrue(db.decide_approval('a', 'accept', 'feishu:same-message'))
        self.assertFalse(db.decide_approval('b', 'accept', 'feishu:same-message'))
        self.assertIsNone(db.one('SELECT decision FROM approvals WHERE id=?', ('b',))['decision'])
        db.execute("UPDATE jobs SET status='completed' WHERE id='b'")
        self.assertFalse(db.decide_approval('b', 'accept', 'feishu:another-message'))
        self.assertIsNone(db.one('SELECT id FROM channel_events WHERE id=?', ('feishu:another-message',)))

    def test_web_and_question_endpoints_reject_second_decision(self):
        from workstation import server
        from fastapi import HTTPException
        self.assertTrue(db.decide_approval('a', 'accept'))
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.decide('a', server.Decision(decision='decline')))
        self.assertEqual(caught.exception.status_code, 409)
        db.execute("UPDATE approvals SET method='item/tool/requestUserInput',payload=? WHERE id='b'",
                   (json.dumps({'questions': [{'id': 'q'}]}),))
        answers = server.Answers(answers={'q': ['yes']})
        self.assertEqual(asyncio.run(server.answer_questions('b', answers)), {'ok': True})
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server.answer_questions('b', answers))
        self.assertEqual(caught.exception.status_code, 409)

    def test_feishu_group_authorization_and_delivery_retries(self):
        config = {'verification_token': 'test-token', 'target_chat_id': 'group-a'}
        payload = {
            'header': {'token': 'test-token', 'event_id': 'delivery-1'},
            'event': {
                'sender': {'sender_id': {'open_id': 'member-a'}},
                'message': {'chat_id': 'group-a', 'content': json.dumps({'text': '/approve a'})},
            },
        }
        with patch.object(feishu_relay, 'load_feishu_config', return_value=config):
            self.assertTrue(asyncio.run(feishu_relay.handle_webhook_payload(payload))['handled'])
            payload['event']['message']['content'] = json.dumps({'text': '/approve b'})
            asyncio.run(feishu_relay.handle_webhook_payload(payload))
            self.assertIsNone(db.one("SELECT decision FROM approvals WHERE id='b'")['decision'])
            payload['header']['event_id'] = 'delivery-2'
            payload['event']['message']['chat_id'] = 'other-group'
            self.assertFalse(asyncio.run(feishu_relay.handle_webhook_payload(payload))['ok'])
            payload['event']['message']['chat_id'] = 'group-a'
            payload['event']['message']['content'] = json.dumps({'text': '/approve'})
            result = asyncio.run(feishu_relay.handle_webhook_payload(payload))
            self.assertIn('审批单号', result['reply'])
            self.assertIsNone(db.one("SELECT decision FROM approvals WHERE id='b'")['decision'])
            payload['header'].pop('token')
            with self.assertRaises(PermissionError):
                asyncio.run(feishu_relay.handle_webhook_payload(payload))

    def test_feishu_card_envelope_honors_operator_allowlist(self):
        config = {'verification_token': 'test-token', 'allowed_open_ids': ['operator-a']}
        payload = {
            'header': {'token': 'test-token', 'event_id': 'card-event'},
            'event': {
                'operator': {'operator_id': {'open_id': 'operator-b'}},
                'action': {'value': {'decision': 'accept', 'approval_id': 'a'}},
            },
        }
        with patch.object(feishu_relay, 'load_feishu_config', return_value=config):
            self.assertFalse(asyncio.run(feishu_relay.handle_webhook_payload(payload))['ok'])
            payload['event']['operator']['operator_id']['open_id'] = 'operator-a'
            self.assertTrue(asyncio.run(feishu_relay.handle_webhook_payload(payload))['ok'])

    def test_snapshot_mirror_and_import_failures_are_reported(self):
        snapshot_path = Path(self.temp.name) / 'snapshot.tar.gz'
        snapshot_path.write_bytes(b'snapshot')
        snapshot = {'path': str(snapshot_path), 'filename': 'rove-snapshot-test.tar.gz', 'size_formatted': '8 B'}
        cluster_sync.update_cluster_config({'peer_url': '', 'pve_ssh_host': 'mock-pve'})
        cases = [
            ([subprocess.CompletedProcess([], 1, b'ROVE_PRIMARY_OK\n', b'disk full')], '备盘复制'),
            ([subprocess.CompletedProcess([], 0, b'', b''), subprocess.CompletedProcess([], 1, b'', b'import error')], '快照导入'),
            ([subprocess.TimeoutExpired('ssh', 180)], '失败'),
        ]
        for results, expected in cases:
            with self.subTest(expected=expected), patch.object(cluster_sync, 'create_snapshot', return_value=snapshot), patch.object(cluster_sync.subprocess, 'run', side_effect=results):
                with self.assertRaisesRegex(RuntimeError, expected):
                    cluster_sync.push_snapshot_to_peer(include_projects=False)
                status = cluster_sync.get_cluster_config()['last_sync_status']
                self.assertIn('失败', status)
        results = [subprocess.CompletedProcess([], 0, b'', b''), subprocess.CompletedProcess([], 0, b'ROVE_IMPORT_SKIPPED\n', b'')]
        with patch.object(cluster_sync, 'create_snapshot', return_value=snapshot), patch.object(cluster_sync.subprocess, 'run', side_effect=results):
            result = cluster_sync.push_snapshot_to_peer(include_projects=False)
            self.assertEqual(result['import_status'], 'skipped')
            self.assertIn('导入已跳过', result['detail'])

    def test_remote_copy_script_handles_spaces_and_real_disk_failure(self):
        snapshot_path = Path(self.temp.name) / 'source.tar.gz'
        snapshot_path.write_bytes(b'snapshot-content')
        snapshot = {'path': str(snapshot_path), 'filename': 'rove-snapshot-test.tar.gz', 'size_formatted': '16 B'}
        primary = Path(self.temp.name) / 'primary disk'
        secondary = Path(self.temp.name) / 'secondary disk'
        cluster_sync.update_cluster_config({
            'peer_url': '', 'pve_ssh_host': 'mock-pve', 'pve_ctid': '',
            'remote_data_dir': str(primary), 'pve_secondary_backup_dir': str(secondary),
        })
        real_run = subprocess.run
        def run_locally(command, **kwargs):
            if '/opt/walking-workstation/.venv/bin/python' in command[-1]:
                return subprocess.CompletedProcess(command, 0, b'ROVE_IMPORT_SKIPPED', b'')
            return real_run(['/bin/sh', '-c', command[-1]], **kwargs)
        with patch.object(cluster_sync, 'create_snapshot', return_value=snapshot), patch.object(cluster_sync.subprocess, 'run', side_effect=run_locally):
            result = cluster_sync.push_snapshot_to_peer(include_projects=False)
            self.assertEqual(Path(result['remote_path']).read_bytes(), b'snapshot-content')
            self.assertEqual(Path(result['secondary_path']).read_bytes(), b'snapshot-content')
            self.assertEqual(Path(result['secondary_path']).stat().st_mode & 0o777, 0o600)
            blocked_secondary = Path(self.temp.name) / 'blocked disk'
            blocked_secondary.write_text('a file blocks directory creation')
            cluster_sync.update_cluster_config({'pve_secondary_backup_dir': str(blocked_secondary)})
            with self.assertRaisesRegex(RuntimeError, '备盘复制'):
                cluster_sync.push_snapshot_to_peer(include_projects=False)



if __name__ == '__main__':
    unittest.main()
