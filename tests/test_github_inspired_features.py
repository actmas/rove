import http.server
import json
from pathlib import Path
import secrets
import socketserver
import subprocess
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from workstation import server
from workstation import store as db
from workstation import worktree


class GithubInspiredFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orig_data = db.DATA
        db.DATA = Path(self.temp.name) / 'data'
        self.project = Path(self.temp.name) / 'demo_repo'
        self.project.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init'], cwd=self.project, check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.project, check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=self.project, check=True, capture_output=True)
        (self.project / '.gitignore').write_text('.env\nnode_modules/\n', encoding='utf-8')
        (self.project / 'main.py').write_text("print('init')\n", encoding='utf-8')
        subprocess.run(['git', 'add', '-A'], cwd=self.project, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=self.project, check=True, capture_output=True)

        db.init()
        cfg = db.config()
        cfg['allowed_roots'] = [str(Path(self.temp.name).resolve())]
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

    def put(self, path, body=None):
        return self.client.put('/api/' + path, json=body or {}, headers=self.headers)

    def test_1_worktree_bootstrap_env_and_worktreeinclude(self):
        (self.project / '.env').write_text('API_MODE=dev\n', encoding='utf-8')
        (self.project / '.worktreeinclude').write_text('local_notes.txt\n', encoding='utf-8')
        (self.project / 'local_notes.txt').write_text('keep me\n', encoding='utf-8')
        (self.project / 'node_modules').mkdir()
        (self.project / 'node_modules' / 'pkg.json').write_text('{}', encoding='utf-8')

        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()
        res = worktree.create_session_worktree(proj, sess)

        wt = Path(res['path'])
        self.assertTrue((wt / '.env').is_file())
        self.assertEqual((wt / 'local_notes.txt').read_text(encoding='utf-8'), 'keep me\n')
        self.assertTrue((wt / 'node_modules').is_symlink())

    def test_2_live_file_edit_and_suggest_commit(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        save = self.put(f"projects/{proj['id']}/file", {'path': 'main.py', 'text': "print('edited')\n"})
        self.assertEqual(save.status_code, 200, save.text)
        self.assertEqual((self.project / 'main.py').read_text(encoding='utf-8'), "print('edited')\n")

        sugg = self.post(f"projects/{proj['id']}/git/suggest-commit", {})
        self.assertEqual(sugg.status_code, 200, sugg.text)
        self.assertIn('main.py', sugg.json()['message'])

    def test_3_pairing_token_single_use_and_preview_proxy(self):
        tok_res = self.post('auth/pairing-token', {}).json()
        self.assertIn('token', tok_res)

        anon = TestClient(server.app, base_url='http://localhost:8765')
        pair_ok = anon.get(tok_res['pair_path'], follow_redirects=False)
        self.assertEqual(pair_ok.status_code, 303)
        self.assertIn(server.COOKIE, pair_ok.cookies)

        # Replay must fail
        pair_replay = anon.get(tok_res['pair_path'], follow_redirects=False)
        self.assertEqual(pair_replay.status_code, 401)

        # Start tiny local HTTP server on an ephemeral port >= 3000 (excluding 8765)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('X-Frame-Options', 'DENY')
                self.end_headers()
                self.wfile.write(b'<h1>Preview OK</h1>')
            def log_message(self, *args):
                pass

        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                if 3000 <= port <= 9999 and port != 8765:
                    resp = self.get(f'preview/{port}')
                    self.assertEqual(resp.status_code, 200)
                    self.assertIn('Preview OK', resp.text)
                    self.assertEqual(resp.headers.get('x-frame-options'), 'SAMEORIGIN')
            finally:
                httpd.shutdown()

        bad_port = self.get('preview/22')
        self.assertEqual(bad_port.status_code, 400)

    def test_5_queue_pause_resume_respects_waiting_job(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()

        # Add a queued message
        self.post(f"sessions/{sess['id']}/queue", {'text': 'future task'})

        # Simulate job waiting for approval
        jid = secrets.token_hex(12)
        db.execute(
            "INSERT INTO jobs(id,session_id,request_id,prompt,status,started,engine) VALUES(?,?,?,?,'waiting',?,'codex')",
            (jid, sess['id'], secrets.token_hex(8), 'needs approval', 1000.0),
        )

        # Pause queue then resume
        self.post(f"sessions/{sess['id']}/queue/pause", {'paused': True})
        res = self.post(f"sessions/{sess['id']}/queue/pause", {'paused': False})
        self.assertEqual(res.status_code, 200)
        # Because job is 'waiting', it should NOT dispatch another job and crash with 500
        self.assertIsNone(res.json()['dispatched_job_id'])

    def test_6_session_deletion_cleans_checkpoints(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        sess = self.post('sessions', {'project_id': proj['id'], 'engine': 'codex'}).json()
        jid = secrets.token_hex(12)

        from workstation import checkpoints
        ref = checkpoints.capture_checkpoint(str(self.project), sess['id'], jid)
        self.assertTrue(ref.startswith('refs/rove/checkpoints/'))

        # Check ref exists
        res = subprocess.run(['git', 'rev-parse', '--verify', ref], cwd=str(self.project), capture_output=True)
        self.assertEqual(res.returncode, 0)

        # Delete session
        del_res = self.client.delete(f"/api/sessions/{sess['id']}", headers=self.headers)
        self.assertEqual(del_res.status_code, 200)

        # Verify checkpoint ref was deleted
        res2 = subprocess.run(['git', 'rev-parse', '--verify', ref], cwd=str(self.project), capture_output=True)
        self.assertNotEqual(res2.returncode, 0)

    def test_4_arena_dual_worktree_and_adopt_winner(self):
        proj = self.post('projects', {'path': str(self.project)}).json()
        # Mock spawn so test doesn't spawn detached OS subprocesses
        orig_spawn = server.spawn
        server.spawn = lambda *args, **kwargs: None
        try:
            started = self.post('arena/start', {
                'project_id': proj['id'],
                'prompt': 'Add feature X',
                'engine_a': 'codex',
                'engine_b': 'antigravity',
            })
            self.assertEqual(started.status_code, 201, started.text)
            arena_data = started.json()

            # Simulate candidate A writing a file in its worktree
            sess_a = db.one('SELECT * FROM sessions WHERE id=?', (arena_data['session_a_id'],))
            wt_a = Path(sess_a['worktree_path'])
            (wt_a / 'feature_a.py').write_text('x = 1\n', encoding='utf-8')
            db.execute("UPDATE jobs SET status='completed' WHERE session_id IN (?,?)", (arena_data['session_a_id'], arena_data['session_b_id']))

            status = self.get(f"arena/{arena_data['id']}").json()
            self.assertEqual(status['status'], 'ready')
            self.assertGreater(status['candidate_a']['diff']['additions'], 0)

            sess_b = db.one('SELECT * FROM sessions WHERE id=?', (arena_data['session_b_id'],))
            wt_b_path = Path(sess_b['worktree_path'])
            self.assertTrue(wt_b_path.exists())

            adopt = self.post(f"arena/{arena_data['id']}/adopt", {'winner_session_id': arena_data['session_a_id']})
            self.assertEqual(adopt.status_code, 200, adopt.text)
            self.assertTrue((self.project / 'feature_a.py').is_file())

            # Verify loser worktree was cleaned up
            self.assertFalse(wt_b_path.exists())
            sess_b_after = db.one('SELECT * FROM sessions WHERE id=?', (arena_data['session_b_id'],))
            self.assertIsNone(sess_b_after['worktree_path'])
            self.assertEqual(sess_b_after['use_worktree'], 0)
        finally:
            server.spawn = orig_spawn


if __name__ == '__main__':
    unittest.main()
