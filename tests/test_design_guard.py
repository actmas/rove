import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

tmp_data = Path(tempfile.mkdtemp(prefix='workstation-design-data-'))
os.environ['WORKSTATION_DATA'] = str(tmp_data)

from workstation import store as db
from workstation import design_guard
from workstation import memories
from workstation.server import app


class DesignGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data = db.DATA
        cls.tmp_data = Path(tempfile.mkdtemp(prefix='workstation-design-data-'))
        db.DATA = cls.tmp_data
        db.init()
        cls.client = TestClient(app, base_url='http://localhost:8765')
        cls.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        auth = json.loads((db.DATA / 'auth.json').read_text())
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

    def test_ui_task_detection(self):
        self.assertTrue(design_guard.is_ui_task('为设置页增加一个深色模式切换按钮'))
        self.assertTrue(design_guard.is_ui_task('重构前端弹窗组件与布局样式'))
        self.assertTrue(design_guard.is_ui_task('Fix React modal z-index and button colors'))
        self.assertFalse(design_guard.is_ui_task('修复 SQLite WAL 数据库并发写锁超时'))

    def test_anti_slop_directive(self):
        directive = design_guard.get_design_directive(prompt_text='设计一个登录表单卡片')
        self.assertIn('Anti-AI-Slop', directive)
        self.assertIn('色彩与表面', directive)
        self.assertIn('状态机与无障碍', directive)

    def test_code_audit(self):
        bad_code = """
        <div className="bg-gradient-to-r from-purple-500 to-pink-500 rounded-3xl" style="box-shadow: 0 25px 50px rgba(0,0,0,0.6)">
          <h1 style="color: #888">AI Title</h1>
        </div>
        """
        findings = design_guard.audit_code_changes(bad_code)
        self.assertTrue(len(findings) >= 2)
        rule_ids = [f['rule_id'] for f in findings]
        self.assertIn('cliche_gradient', rule_ids)
        self.assertIn('heavy_shadow', rule_ids)

        clean_code = """
        <div className="card border border-subtle rounded-xl p-4">
          <h1 className="text-primary font-semibold">Clean Title</h1>
        </div>
        """
        clean_findings = design_guard.audit_code_changes(clean_code)
        self.assertEqual(len(clean_findings), 0)

    def test_api_endpoints(self):
        proj = db.query('SELECT * FROM projects')[0]

        # GET /api/design/standard
        res = self.client.get('/api/design/standard', headers=self.auth_headers())
        self.assertEqual(res.status_code, 200)
        self.assertIn('Anti-AI-Slop', res.json()['directive'])

        # POST /api/projects/{id}/design/seed
        seed_res = self.client.post(f'/api/projects/{proj["id"]}/design/seed', headers=self.auth_headers())
        self.assertEqual(seed_res.status_code, 200)
        mem = seed_res.json()['memory']
        self.assertTrue(mem['pinned'])
        self.assertIn('Anti-AI-Slop', mem['content'])

        # Check memory exists in project memories
        mems = memories.list_memories(proj['id'])
        self.assertTrue(any('Anti-AI-Slop' in m['content'] for m in mems))

        # POST /api/design/audit
        audit_res = self.client.post(
            '/api/design/audit',
            json={'code': 'div { box-shadow: 0 30px 40px rgba(0,0,0,0.5); }'},
            headers=self.auth_headers(),
        )
        self.assertEqual(audit_res.status_code, 200)
        self.assertFalse(audit_res.json()['clean'])
        self.assertTrue(len(audit_res.json()['findings']) > 0)


if __name__ == '__main__':
    unittest.main()
