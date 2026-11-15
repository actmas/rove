import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

tmp_data = Path(tempfile.mkdtemp(prefix='workstation-feishu-data-'))
os.environ['WORKSTATION_DATA'] = str(tmp_data)

from workstation import store as db
from workstation import feishu_relay
from workstation.server import app


class FeishuRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_data = db.DATA
        cls.tmp_data = Path(tempfile.mkdtemp(prefix='workstation-feishu-data-'))
        db.DATA = cls.tmp_data
        db.init()
        cls.client = TestClient(app, base_url='http://localhost:8765')
        cls.headers = {'X-Workstation': '1', 'Origin': 'http://localhost:8765'}
        auth = json.loads((db.DATA / 'auth.json').read_text())
        plain = (db.DATA / 'access-password.txt').read_text().strip()
        login_res = cls.client.post('/api/login', json={'password': plain}, headers=cls.headers)
        assert login_res.status_code == 200, login_res.text
        cls.cookie = login_res.cookies['walking_session']

        # Setup mock feishu config
        cls.feishu_cfg_path = db.DATA / 'feishu.json'
        cls.mock_feishu_cfg = {
            'app_id': 'cli_test_123456',
            'app_secret': 'sec_test_secret',
            'allowed_open_ids': ['ou_test_user_openid'],
        }
        cls.feishu_cfg_path.write_text(json.dumps(cls.mock_feishu_cfg))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        db.DATA = cls._orig_data
        shutil.rmtree(cls.tmp_data, ignore_errors=True)
        shutil.rmtree(tmp_data, ignore_errors=True)

    def auth_headers(self):
        return dict(self.headers)

    def test_feishu_config_and_status(self):
        cfg = feishu_relay.load_feishu_config()
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg['app_id'], 'cli_test_123456')

        st = feishu_relay.get_feishu_status()
        self.assertTrue(st['configured'])
        self.assertTrue(st['has_target'])
        self.assertEqual(st['target_type'], 'open_id')

    @patch.object(feishu_relay.FeishuAPI, 'send_text')
    def test_approval_notification_formatting(self, mock_send):
        async def fake_send(receive_id, text, receive_id_type='open_id'):
            self.assertEqual(receive_id, 'ou_test_user_openid')
            self.assertIn('任务待审批', text)
            self.assertIn('/approve app12345', text)
            self.assertIn('/reject app12345', text)
            return {'code': 0}

        mock_send.side_effect = fake_send

        session = {'id': 'sess1', 'title': '重构路由'}
        project = {'name': 'Rove'}
        params = {'command': 'pnpm build'}

        import asyncio
        ok = asyncio.run(feishu_relay.send_approval_notification(session, project, 'app12345', 'item/commandExecution/requestApproval', params))
        self.assertTrue(ok)
        self.assertEqual(mock_send.call_count, 1)

    @patch.object(feishu_relay.FeishuAPI, 'send_text')
    def test_outcome_notification_formatting(self, mock_send):
        async def fake_send(receive_id, text, receive_id_type='open_id'):
            self.assertIn('任务完成', text)
            self.assertIn('测试项目', text)
            return {'code': 0}

        mock_send.side_effect = fake_send

        session = {'id': 'sess2', 'title': '自动化测试'}
        project = {'name': '测试项目'}
        outcome = {'status': 'completed', 'error': None}

        import asyncio
        ok = asyncio.run(feishu_relay.send_outcome_notification('job1', session, project, outcome))
        self.assertTrue(ok)
        self.assertEqual(mock_send.call_count, 1)

    def test_api_status_endpoint(self):
        res = self.client.get('/api/feishu/status', headers=self.auth_headers())
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['configured'])
        self.assertTrue(res.json()['has_target'])


if __name__ == '__main__':
    unittest.main()
