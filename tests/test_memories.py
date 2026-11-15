import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

tmp_data = Path(tempfile.mkdtemp(prefix='workstation-mem-test-'))
os.environ['WORKSTATION_DATA'] = str(tmp_data)

from workstation import store as db
from workstation import memories
from workstation.server import app


class MemoriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data = db.DATA
        cls.tmp_data = Path(tempfile.mkdtemp(prefix='workstation-mem-test-'))
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

    def test_memories_crud_and_sorting(self):
        projects = db.query('SELECT * FROM projects')
        self.assertTrue(len(projects) > 0)
        proj = projects[0]

        # 1. Add memories
        m1 = memories.add_memory(proj['id'], '统一使用 pnpm，切勿使用 npm', category='convention', pinned=False)
        self.assertIsNotNone(m1['id'])
        self.assertEqual(m1['category'], 'convention')
        self.assertEqual(m1['pinned'], 0)

        m2 = memories.add_memory(proj['id'], '状态存储使用 SQLite WAL', category='architecture', pinned=True)
        self.assertEqual(m2['pinned'], 1)

        # 2. List memories: pinned should come first
        items = memories.list_memories(proj['id'])
        self.assertTrue(len(items) >= 2)
        self.assertEqual(items[0]['id'], m2['id'], 'Pinned memory should be first')

        # 3. Deduplication check
        m1_dup = memories.add_memory(proj['id'], '统一使用 pnpm，切勿使用 npm', category='convention', pinned=True)
        self.assertEqual(m1_dup['id'], m1['id'])
        self.assertEqual(m1_dup['pinned'], 1)

        # 4. Update memory
        updated = memories.update_memory(m1['id'], content='统一使用 pnpm 严格管理依赖', category='tooling', pinned=False)
        self.assertEqual(updated['content'], '统一使用 pnpm 严格管理依赖')
        self.assertEqual(updated['category'], 'tooling')
        self.assertEqual(updated['pinned'], 0)

        # 5. Build context
        context = memories.build_memory_context(proj['id'])
        self.assertIn('[项目持久上下文与全局约定', context)
        self.assertIn('统一使用 pnpm 严格管理依赖', context)
        self.assertIn('状态存储使用 SQLite WAL', context)
        self.assertIn('📌', context)

        # 6. Delete
        del_ok = memories.delete_memory(m1['id'])
        self.assertTrue(del_ok)
        self.assertIsNone(memories.get_memory(m1['id']))

    def test_extract_explicit_memories(self):
        text = (
            "请记住：测试命令统一使用 python -m unittest discover -s tests -v\n"
            "架构决策：所有写事务必须走 db.transaction()\n"
            "严禁：直接修改 .config 目录下的私密凭证\n"
            "能不能让交接的内容在后台自动完成，不要单独显示出来，这样感觉切换不自然\n"
        )
        extracted = memories.extract_explicit_memories(text)
        self.assertEqual(len(extracted), 3)
        cats = [e['category'] for e in extracted]
        self.assertIn('convention', cats)
        self.assertIn('architecture', cats)
        self.assertIn('constraint', cats)
        for item in extracted:
            self.assertNotIn('单独显示出来', item['content'])

    def test_api_endpoints(self):
        proj = db.query('SELECT * FROM projects')[0]

        # GET /api/projects/{id}/memories
        res = self.client.get(
            f'/api/projects/{proj["id"]}/memories',
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn('memories', data)
        self.assertIn('categories', data)

        # POST /api/projects/{id}/memories
        create_res = self.client.post(
            f'/api/projects/{proj["id"]}/memories',
            json={'content': '禁止引入重型外部依赖', 'category': 'constraint', 'pinned': True},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(create_res.status_code, 200)
        created = create_res.json()
        self.assertEqual(created['content'], '禁止引入重型外部依赖')
        self.assertEqual(created['pinned'], 1)

        # PATCH /api/memories/{id}
        patch_res = self.client.patch(
            f'/api/memories/{created["id"]}',
            json={'pinned': False, 'category': 'convention'},
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()['pinned'], 0)
        self.assertEqual(patch_res.json()['category'], 'convention')

        # DELETE /api/memories/{id}
        del_res = self.client.delete(
            f'/api/memories/{created["id"]}',
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()['ok'])

        # State endpoint includes memories_count
        state_res = self.client.get(
            '/api/state',
            headers=self.auth_headers(),
            cookies={'walking_session': self.cookie},
        )
        self.assertEqual(state_res.status_code, 200)
        st = state_res.json()
        self.assertIn('memory_categories', st)
        target_p = next(p for p in st['projects'] if p['id'] == proj['id'])
        self.assertIn('memories_count', target_p)


if __name__ == '__main__':
    unittest.main()
