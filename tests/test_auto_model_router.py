"""Tests for task-aware & 7-day expiring-quota adaptive model/effort auto-selection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from workstation import engines, model_router, quotas, server
from workstation import store as db


class AutoModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orig_data = db.DATA
        self.prev_env = os.environ.get('WORKSTATION_DATA')
        os.environ['WORKSTATION_DATA'] = str(Path(self.temp.name) / 'data')
        db.close_all_connections()
        db.DATA = Path(os.environ['WORKSTATION_DATA'])
        self.project = Path(self.temp.name) / 'repo'
        self.project.mkdir(parents=True, exist_ok=True)
        db.init()
        cfg = db.config()
        cfg['allowed_roots'] = [str(Path(self.temp.name).resolve())]
        # Create dummy executable bins so agents.is_agent_installed returns True for both engines
        bin_dir = Path(self.temp.name) / 'bin'
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name in ('codex', 'agy'):
            p = bin_dir / name
            p.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            p.chmod(0o755)
        cfg['codex_bin'] = str(bin_dir / 'codex')
        cfg['antigravity_bin'] = str(bin_dir / 'agy')
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

    def post(self, path, payload=None):
        return self.client.post(f'/api/{path}', json=payload or {}, headers=self.headers)

    def patch(self, path, payload=None):
        return self.client.patch(f'/api/{path}', json=payload or {}, headers=self.headers)

    def test_1_task_complexity_classification(self):
        simple = model_router.classify_task('解释一下 worker.py 这个文件怎么用')
        self.assertEqual(simple['complexity'], 'simple')
        self.assertEqual(simple['desired_tier'], 'fast')
        self.assertEqual(simple['base_effort'], 'low')

        standard = model_router.classify_task('修复 checkpoints.py 里的参数校验问题并补充边界测试用例')
        self.assertEqual(standard['complexity'], 'standard')
        self.assertEqual(standard['desired_tier'], 'balanced')
        self.assertEqual(standard['base_effort'], 'medium')

        complex_task = model_router.classify_task(
            '全面审计重构 worker.py 的多引擎并发调度架构，解决竞态条件和死锁安全漏洞',
            mode='plan',
        )
        self.assertEqual(complex_task['complexity'], 'complex')
        self.assertEqual(complex_task['desired_tier'], 'flagship')
        self.assertIn(complex_task['base_effort'], ('high', 'xhigh'))

    def test_2_prioritizes_expiring_7day_quota_and_scales_up(self):
        now = 1_800_000_000.0
        # Codex 7d resets in 6 days (144h) with 90% left -> low urgency (expected ~85%, surplus +5%)
        # Antigravity Argon 7d resets in 12 hours with 70% left -> VERY high expiring urgency!
        q_snap = {
            'codex': {
                'status': 'ready',
                'primary': {'remaining_percent': 95, 'resets_at': int(now + 4 * 3600)},
                'secondary': {'remaining_percent': 90, 'resets_at': int(now + 144 * 3600)},
            },
            'antigravity': {
                'status': 'ready',
                'gemini_4_argon': {
                    'weekly': {'remaining_percent': 70, 'resets_at': int(now + 12 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
                'general_flash': {
                    'weekly': {'remaining_percent': 85, 'resets_at': int(now + 130 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
                'claude_gpt': {
                    'weekly': {'remaining_percent': 80, 'resets_at': int(now + 130 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
            },
        }
        res = model_router.auto_select_model_and_effort(
            prompt='给 server.py 添加一个新的 API 接口和对应测试',
            mode='standard',
            requested_engine='auto',
            requested_model='auto',
            requested_effort='auto',
            quotas_snapshot=q_snap,
            installed_engines=['codex', 'antigravity'],
            now=now,
        )
        self.assertEqual(res['engine'], 'antigravity')
        self.assertIn('argon', res['model'])
        self.assertEqual(res['budget_state'], 'expiring_abundant')
        # Standard task upgraded to flagship + high effort because 7d quota is about to expire unused
        self.assertEqual(res['effort'], 'high')

        # Flip scenario: Codex 7d resets in 8 hours with 60% left; Antigravity resets in 5 days
        q_snap_codex_expiring = {
            'codex': {
                'status': 'ready',
                'primary': {'remaining_percent': 85, 'resets_at': int(now + 3 * 3600)},
                'secondary': {'remaining_percent': 60, 'resets_at': int(now + 8 * 3600)},
            },
            'antigravity': {
                'status': 'ready',
                'gemini_4_argon': {
                    'weekly': {'remaining_percent': 90, 'resets_at': int(now + 120 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
            },
        }
        res2 = model_router.auto_select_model_and_effort(
            prompt='排查 worker.py 队列分发逻辑的问题',
            mode='standard',
            requested_engine='auto',
            requested_model='auto',
            requested_effort='auto',
            quotas_snapshot=q_snap_codex_expiring,
            installed_engines=['codex', 'antigravity'],
            now=now,
        )
        self.assertEqual(res2['engine'], 'codex')
        self.assertEqual(res2['budget_state'], 'expiring_abundant')
        self.assertEqual(res2['model'], 'gpt-6-astra')
        self.assertIn(res2['effort'], ('high', 'xhigh'))

    def test_3_intra_engine_pool_switch_and_scarcity_throttling(self):
        now = 1_800_000_000.0
        # Inside Antigravity alone: Argon 7d is scarce (12%, 5d left), while Claude 7d resets in 16h (82% left)
        q_agy = {
            'antigravity': {
                'status': 'ready',
                'gemini_4_argon': {
                    'weekly': {'remaining_percent': 12, 'resets_at': int(now + 120 * 3600)},
                    'rolling_5h': {'remaining_percent': 90, 'resets_at': int(now + 3 * 3600)},
                },
                'claude_gpt': {
                    'weekly': {'remaining_percent': 82, 'resets_at': int(now + 16 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
                'general_flash': {
                    'weekly': {'remaining_percent': 45, 'resets_at': int(now + 96 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 4 * 3600)},
                },
            }
        }
        res_agy = model_router.auto_select_model_and_effort(
            prompt='架构重设计：拆分核心状态存储引擎并保证事务安全',
            mode='plan',
            requested_engine='antigravity',
            requested_model='auto',
            requested_effort='auto',
            quotas_snapshot=q_agy,
            now=now,
        )
        self.assertEqual(res_agy['engine'], 'antigravity')
        self.assertIn('claude', res_agy['model'])

        # Pinned to Codex while Codex 7d quota is scarce (10%, 5d left) -> throttles model, effort, and disables fast
        q_codex_scarce = {
            'codex': {
                'status': 'ready',
                'primary': {'remaining_percent': 50, 'resets_at': int(now + 3 * 3600)},
                'secondary': {'remaining_percent': 10, 'resets_at': int(now + 120 * 3600)},
            }
        }
        res_scarce = model_router.auto_select_model_and_effort(
            prompt='全面审计系统架构并优化并发性能',
            mode='standard',
            requested_engine='codex',
            requested_model='auto',
            requested_effort='auto',
            requested_fast=True,
            quotas_snapshot=q_codex_scarce,
            now=now,
        )
        self.assertEqual(res_scarce['budget_state'], 'scarce')
        self.assertNotEqual(res_scarce['model'], 'gpt-6-astra')
        self.assertIn(res_scarce['effort'], ('low', 'medium'))
        self.assertFalse(res_scarce['fast'])

    def test_4_api_end_to_end_preserves_auto_session_prefs(self):
        now = time.time()
        quotas.save_cached_quotas({
            'updated_at': now,
            'codex': {
                'status': 'ready',
                'primary': {'remaining_percent': 85, 'resets_at': int(now + 3600)},
                'secondary': {'remaining_percent': 75, 'resets_at': int(now + 10 * 3600)},
            },
            'antigravity': {
                'status': 'ready',
                'gemini_4_argon': {
                    'weekly': {'remaining_percent': 40, 'resets_at': int(now + 120 * 3600)},
                    'rolling_5h': {'remaining_percent': 100, 'resets_at': int(now + 3600)},
                },
            },
        })

        # Create session in full auto mode
        sess_res = self.post('sessions', {
            'project_id': 'proj1',
            'engine': 'auto',
            'model': 'auto',
            'effort': 'auto',
        })
        self.assertEqual(sess_res.status_code, 200, sess_res.text)
        sid = sess_res.json()['id']

        # Preview endpoint reflects 7-day expiring Codex choice
        prev = self.post('models/auto-select', {
            'prompt': '给 api 模块补充单元测试并修复边界问题',
            'engine': 'auto',
            'model': 'auto',
            'effort': 'auto',
        })
        self.assertEqual(prev.status_code, 200, prev.text)
        pdata = prev.json()
        self.assertEqual(pdata['engine'], 'codex')
        self.assertEqual(pdata['budget_state'], 'expiring_abundant')

        # Submit message -> job stores concrete selection, session keeps 'auto'
        orig_spawn = server.spawn
        server.spawn = lambda *_args, **_kwargs: None
        try:
            msg_res = self.post(f'sessions/{sid}/messages', {
                'text': '分析并重构登录校验流程的并发安全问题',
                'request_id': secrets.token_hex(12),
                'engine': 'auto',
                'model': 'auto',
                'effort': 'auto',
            })
            self.assertEqual(msg_res.status_code, 200, msg_res.text)
            job = msg_res.json()
            self.assertEqual(job['engine'], 'codex')
            self.assertNotEqual(job['model'], 'auto')
            self.assertIn(job['effort'], ('high', 'xhigh'))

            sess_after = db.one('SELECT * FROM sessions WHERE id=?', (sid,))
            self.assertEqual(sess_after['engine'], 'auto')
            self.assertEqual(sess_after['model'], 'auto')
            self.assertEqual(sess_after['effort'], 'auto')

            ev = db.one("SELECT payload FROM events WHERE job_id=? AND kind='user'", (job['id'],))
            ev_payload = json.loads(ev['payload'])
            self.assertTrue(ev_payload.get('auto_routed'))
            self.assertTrue(ev_payload.get('route_reason'))

            # Patching session keeps auto preferences cleanly via resolve_model(..., allow_auto=True)
            patch_res = self.patch(f'sessions/{sid}', {'engine': 'codex', 'model': 'auto', 'effort': 'auto'})
            self.assertEqual(patch_res.status_code, 200, patch_res.text)
            self.assertEqual(patch_res.json()['model'], 'auto')
            self.assertEqual(patch_res.json()['effort'], 'auto')
        finally:
            server.spawn = orig_spawn


if __name__ == '__main__':
    unittest.main()
