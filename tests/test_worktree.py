import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

tmp_data = Path(tempfile.mkdtemp(prefix='workstation-wt-data-'))
os.environ['WORKSTATION_DATA'] = str(tmp_data)

from workstation import store as db
from workstation import worktree
from workstation.server import app


class WorktreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data = db.DATA
        cls.tmp_data = Path(tempfile.mkdtemp(prefix='workstation-wt-data-'))
        db.DATA = cls.tmp_data
        db.init()
        cls.client = TestClient(app, base_url='http://localhost:8765')
        cls.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        auth = json.loads((db.DATA / 'auth.json').read_text())
        plain = (db.DATA / 'access-password.txt').read_text().strip()
        login_res = cls.client.post('/api/login', json={'password': plain}, headers=cls.headers)
        assert login_res.status_code == 200, login_res.text
        cls.cookie = login_res.cookies['walking_session']

        # Setup a dedicated temporary git project
        cls.proj_dir = Path(tempfile.mkdtemp(prefix='workstation-wt-proj-')).resolve()
        subprocess.run(['git', 'init'], cwd=str(cls.proj_dir), check=True, capture_output=True)
        (cls.proj_dir / 'main.py').write_text("print('hello main')\n")
        subprocess.run(['git', 'add', '.'], cwd=str(cls.proj_dir), check=True)
        subprocess.run(['git', '-c', 'user.name=Tester', '-c', 'user.email=test@local', 'commit', '-m', 'Initial commit'], cwd=str(cls.proj_dir), check=True)

        cfg = db.config()
        cfg['allowed_roots'].append(str(cls.proj_dir.parent))
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))

        cls.proj_id = secrets.token_hex(8)
        db.execute('INSERT INTO projects(id, name, path) VALUES(?,?,?)', (cls.proj_id, 'wt-test-repo', str(cls.proj_dir)))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        db.DATA = cls._orig_data
        shutil.rmtree(cls.proj_dir, ignore_errors=True)
        shutil.rmtree(cls.tmp_data, ignore_errors=True)
        shutil.rmtree(tmp_data, ignore_errors=True)

    def auth_headers(self):
        return dict(self.headers)

    def new_session(self):
        sid = secrets.token_hex(12)
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id,project_id,engine,title,native_id,created,updated,mode,fast,pinned) VALUES(?,?,?,?,NULL,?,?,?,?,0)',
            (sid, self.proj_id, 'codex', 'WT Session', now, now, 'standard', 0),
        )
        return sid

    def test_worktree_lifecycle_and_merge(self):
        sid = self.new_session()
        proj = db.one('SELECT * FROM projects WHERE id=?', (self.proj_id,))
        sess = db.one('SELECT * FROM sessions WHERE id=?', (sid,))

        # 1. Enable worktree
        wt_info = worktree.create_session_worktree(proj, sess)
        self.assertTrue(wt_info['is_worktree'])
        wt_path = Path(wt_info['path'])
        self.assertTrue(wt_path.is_dir())
        self.assertTrue((wt_path / 'main.py').is_file())

        # Verify exclude file has .worktrees
        git_dir = subprocess.run(['git', 'rev-parse', '--git-dir'], cwd=str(self.proj_dir), capture_output=True, text=True).stdout.strip()
        exclude = (self.proj_dir / git_dir / 'info' / 'exclude').read_text()
        self.assertIn('.worktrees', exclude)

        # 2. Check worktree status via API
        status_res = self.client.get(f'/api/sessions/{sid}/worktree', headers=self.auth_headers())
        self.assertEqual(status_res.status_code, 200)
        self.assertTrue(status_res.json()['use_worktree'])
        self.assertEqual(status_res.json()['worktree']['branch'], wt_info['branch'])

        # 3. Modify a file inside the isolated worktree
        (wt_path / 'feature.py').write_text("def new_feature(): return 42\n")

        # Status should report 1 uncommitted file
        sess = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        wt_stat = worktree.get_session_worktree(proj, sess)
        self.assertEqual(wt_stat['uncommitted_files'], 1)

        # 4. Check Diff via API
        diff_res = self.client.get(f'/api/sessions/{sid}/worktree/diff', headers=self.auth_headers())
        self.assertEqual(diff_res.status_code, 200)

        # 5. Merge worktree into main repo
        merge_res = self.client.post(f'/api/sessions/{sid}/worktree/merge', headers=self.auth_headers())
        self.assertEqual(merge_res.status_code, 200)
        self.assertEqual(merge_res.json()['status'], 'merged')

        # Main repository should now have feature.py
        self.assertTrue((self.proj_dir / 'feature.py').is_file())
        self.assertIn('def new_feature', (self.proj_dir / 'feature.py').read_text())

        # 6. Delete worktree
        del_res = self.client.delete(f'/api/sessions/{sid}/worktree', headers=self.auth_headers())
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(wt_path.exists())

    def test_parallel_worktree_concurrency(self):
        s1 = self.new_session()
        s2 = self.new_session()

        proj = db.one('SELECT * FROM projects WHERE id=?', (self.proj_id,))
        sess2 = db.one('SELECT * FROM sessions WHERE id=?', (s2,))

        # Enable worktree for session 2
        worktree.create_session_worktree(proj, sess2)
        sess2 = db.one('SELECT * FROM sessions WHERE id=?', (s2,))

        # Simulate job running on session 1 in main directory
        j1 = secrets.token_hex(12)
        db.execute(
            "INSERT INTO jobs(id,session_id,request_id,prompt,status,started) VALUES(?,?,?,?,'running',?)",
            (j1, s1, secrets.token_hex(16), 'Main task', time.time()),
        )

        # A third session without worktree should be blocked from main directory
        s3 = self.new_session()
        blocked_res = self.client.post(
            f'/api/sessions/{s3}/messages',
            json={'text': 'blocked task', 'request_id': secrets.token_hex(16)},
            headers=self.auth_headers(),
        )
        self.assertEqual(blocked_res.status_code, 409)

        # But Session 2 on isolated worktree should be ALLOWED to launch!
        # Mock worker spawn so tests never launch a live background CLI process.
        with patch('workstation.server.spawn', return_value=999):
            allowed_res = self.client.post(
                f'/api/sessions/{s2}/messages',
                json={'text': 'parallel task', 'request_id': secrets.token_hex(16)},
                headers=self.auth_headers(),
            )
        # Should not be 409 concurrency lock! (It may proceed or return 200)
        self.assertEqual(allowed_res.status_code, 200)


if __name__ == '__main__':
    unittest.main()
