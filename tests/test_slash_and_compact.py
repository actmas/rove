"""Tests for interactive slash command discovery and proactive /compact context rollover."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from workstation import server
from workstation import store as db


class SlashAndCompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orig_data = db.DATA
        self.prev_env = os.environ.get('WORKSTATION_DATA')
        os.environ['WORKSTATION_DATA'] = str(Path(self.temp.name) / 'data')
        db.close_all_connections()
        db.DATA = Path(os.environ['WORKSTATION_DATA'])
        self.project = Path(self.temp.name) / 'repo'
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / '.agent' / 'workflows').mkdir(parents=True, exist_ok=True)
        (self.project / '.agent' / 'workflows' / 'deploy.md').write_text(
            '---\ndescription: 构建并部署服务\n---\n执行部署脚本并校验健康检查。\n',
            encoding='utf-8',
        )
        (self.project / '.claude' / 'commands').mkdir(parents=True, exist_ok=True)
        (self.project / '.claude' / 'commands' / 'lint-fix.md').write_text(
            '# 自动修复代码规范\n运行 linter 并修复所有问题。\n',
            encoding='utf-8',
        )

        db.init()
        cfg = db.config()
        cfg['allowed_roots'] = [str(Path(self.temp.name).resolve())]
        db.write_private(db.DATA / 'config.json', json.dumps(cfg))
        db.execute('INSERT INTO projects(id, name, path) VALUES(?,?,?)', ('proj1', 'Demo', str(self.project)))

        self.client = TestClient(server.app, base_url='http://localhost:8765')
        self.headers = {'Origin': 'http://localhost:8765', 'X-Workstation': '1'}
        pw = (db.DATA / 'access-password.txt').read_text().strip()
        r = self.client.post('/api/login', json={'password': pw}, headers=self.headers)
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        db.close_all_connections()
        db.DATA = self.orig_data
        if self.prev_env is None:
            os.environ.pop('WORKSTATION_DATA', None)
        else:
            os.environ['WORKSTATION_DATA'] = self.prev_env
        shutil.rmtree(self.temp.name, ignore_errors=True)

    def test_1_project_commands_discovers_builtins_and_workflows(self):
        res = self.client.get('/api/projects/proj1/commands', headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        cmds = res.json()['commands']
        names = {c['name']: c for c in cmds}
        self.assertIn('review', names)
        self.assertIn('plan', names)
        self.assertIn('compact', names)
        self.assertIn('commit', names)
        self.assertIn('deploy', names)
        self.assertEqual(names['deploy']['description'], '构建并部署服务')
        self.assertEqual(names['deploy']['source'], 'workflow')
        self.assertIn('lint-fix', names)
        self.assertEqual(names['lint-fix']['source'], 'claude')

    def test_2_compact_resets_native_ids_and_tokens(self):
        s_res = self.client.post('/api/sessions', json={'project_id': 'proj1', 'engine': 'codex'}, headers=self.headers)
        sid = s_res.json()['id']
        db.execute(
            'UPDATE sessions SET native_id=?, native_codex_id=?, tokens_used=150000 WHERE id=?',
            ('thr_old', 'thr_old', sid),
        )
        c_res = self.client.post(f'/api/sessions/{sid}/compact', json={}, headers=self.headers)
        self.assertEqual(c_res.status_code, 200, c_res.text)
        s_after = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
        self.assertIsNone(s_after['native_id'])
        self.assertIsNone(s_after['native_codex_id'])
        self.assertEqual(s_after['tokens_used'], 0)
        ev = db.one("SELECT payload FROM events WHERE session_id=? AND kind='notice' ORDER BY id DESC LIMIT 1", (sid,))
        self.assertIn('/compact', ev['payload'])


if __name__ == '__main__':
    unittest.main()
