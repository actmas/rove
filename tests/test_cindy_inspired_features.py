"""Unit and integration tests for the 7 Cindy-inspired Rove enhancements."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CindyInspiredFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rove-cindy-test-'))
        self.prev_data = os.environ.get('WORKSTATION_DATA')
        os.environ['WORKSTATION_DATA'] = str(self.tmp / 'data')

        from workstation import store as db
        db.close_all_connections()
        db.DATA = Path(os.environ['WORKSTATION_DATA'])
        db.init()
        self.db = db

        self.proj_dir = self.tmp / 'proj'
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / 'app.py').write_text('print("hello")\n', encoding='utf-8')

        self.project_id = 'proj1'
        self.session_id = 'sess1'
        now = time.time()
        db.execute('INSERT INTO projects(id, name, path) VALUES(?,?,?)', (self.project_id, 'TestProj', str(self.proj_dir)))
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, model) VALUES(?,?,?,?,?,?,?)',
            (self.session_id, self.project_id, 'codex', 'Test Session', now, now, 'gpt-5.4'),
        )

    def tearDown(self):
        self.db.close_all_connections()
        if self.prev_data is None:
            os.environ.pop('WORKSTATION_DATA', None)
        else:
            os.environ['WORKSTATION_DATA'] = self.prev_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_1_quota_wait_schedule_cancel_and_resume(self):
        from workstation import quota_wait

        now = 1_700_000_000.0
        q_snap = {'codex': {'primary': {'remaining_percent': 0, 'used_percent': 100, 'resets_at': now + 3600}}}
        row = quota_wait.schedule_quota_wait(
            session_id=self.session_id,
            job_id='job1',
            engine='codex',
            model='gpt-5.4',
            prompt='fix tests',
            has_output=True,
            error_text='Codex 5-hour usage limit reached',
            quotas_snapshot=q_snap,
            now=now,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'waiting')
        self.assertGreater(row['resume_at'], now + 3600)

        # Due processing before resume_at does nothing
        resumed = []
        self.assertEqual(quota_wait.process_due_quota_waits(lambda r: resumed.append(r['id']) or True, now=now + 100), 0)
        # Due processing after resume_at claims and resumes once
        self.assertEqual(quota_wait.process_due_quota_waits(lambda r: resumed.append(r['id']) or True, now=row['resume_at'] + 1), 1)
        self.assertEqual(len(resumed), 1)

        # User cancellation invalidates waiting row
        row2 = quota_wait.schedule_quota_wait(
            session_id=self.session_id,
            job_id='job2',
            engine='codex',
            model='gpt-5.4',
            prompt='fix tests',
            has_output=False,
            error_text='Try again in ~5 min',
            now=now,
        )
        self.assertIsNotNone(row2)
        self.assertEqual(quota_wait.cancel_quota_wait(self.session_id), 1)
        self.assertIsNone(quota_wait.get_active_quota_wait(self.session_id))

    def test_2_independent_review_and_fail_closed_freshness(self):
        from workstation import review

        self.db.event(self.session_id, 'j1', 'user', {'text': '实现登录校验'})
        self.db.event(self.session_id, 'j1', 'codex', {
            'method': 'item/completed',
            'params': {'item': {'id': 'm1', 'type': 'agentMessage', 'text': '已完成登录校验'}},
        })
        prompt = review.build_review_prompt(
            self.db.one('SELECT * FROM sessions WHERE id=?', (self.session_id,)),
            self.db.one('SELECT * FROM projects WHERE id=?', (self.project_id,)),
            focus='安全边界',
        )
        self.assertIn('未经核实的自述', prompt)
        self.assertIn('FINDINGS_COUNT:', prompt)
        self.assertIn('安全边界', prompt)

        rev = review.create_review_record(
            self.db.one('SELECT * FROM sessions WHERE id=?', (self.session_id,)),
            self.db.one('SELECT * FROM projects WHERE id=?', (self.project_id,)),
            'codex',
            'gpt-5.4',
        )
        done = review.complete_review_record(rev['id'], 'P1: missing check\nFINDINGS_COUNT: 1', str(self.proj_dir))
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['findings_count'], 1)

        # Modify workspace -> Fail-Closed marks review stale
        time.sleep(0.02)
        (self.proj_dir / 'app.py').write_text('print("modified")\n', encoding='utf-8')
        checked = review.check_review_freshness(done, str(self.proj_dir))
        self.assertEqual(checked['status'], 'stale')

    def test_3_tool_loop_guard_redaction_and_exemptions(self):
        from workstation import loop_guard

        # Redact before truncate
        raw = 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz1234567890'
        redacted = loop_guard.redact_sensitive_text(raw)
        self.assertNotIn('sk-abcdefghijklmnopqrstuvwxyz1234567890', redacted)

        # Paced polling (>= 30s) is exempt
        g = loop_guard.ToolLoopGuard(consecutive_limit=4)
        for i in range(6):
            t = i * 35_000.0
            g.on_tool_use(f'c{i}', 'commandExecution', {'command': 'pytest'}, now_ms=t)
            v = g.on_tool_result(f'c{i}', 'commandExecution', {'command': 'pytest'}, 'pending', now_ms=t + 100)
            self.assertEqual(v['status'], 'ok')

        # Rapid identical loop triggers suspected + auxiliary STOP
        g2 = loop_guard.ToolLoopGuard(consecutive_limit=4)
        verdict = None
        for i in range(4):
            t = i * 500.0
            g2.on_tool_use(f'r{i}', 'commandExecution', {'command': 'cat missing.txt'}, now_ms=t)
            verdict = g2.on_tool_result(f'r{i}', 'commandExecution', {'command': 'cat missing.txt'}, 'No such file', now_ms=t + 50)
        self.assertEqual(verdict['status'], 'suspected')
        self.assertEqual(loop_guard.review_suspected_loop(verdict, 'fix bug', reviewer_fn=lambda _: 'STOP'), 'STOP')
        self.assertEqual(loop_guard.review_suspected_loop(verdict, 'fix bug', reviewer_fn=None), 'CONTINUE')

    def test_4_zero_trace_secret_input_handoff(self):
        from workstation import input_handoff

        self.db.execute(
            "INSERT INTO jobs(id, session_id, request_id, prompt, status, started) VALUES('jsec', ?, 'reqsec', 'login', 'waiting', ?)",
            (self.session_id, time.time()),
        )
        handoff = input_handoff.create_secret_handoff(
            job_id='jsec',
            session_id=self.session_id,
            purpose='API Key',
            fields=[{'id': 'token', 'label': 'Token', 'isSecret': True}],
        )
        secret_val = 'super-secret-value-999'
        self.assertTrue(input_handoff.submit_secret_values(handoff['id'], {'token': secret_val}))

        # Verify secret never touched SQLite
        row = self.db.one('SELECT * FROM secret_handoffs WHERE id=?', (handoff['id'],))
        self.assertNotIn(secret_val, json.dumps(row))

        # Pop wipes from RAM
        popped = input_handoff.pop_secret_values(handoff['id'])
        self.assertEqual(popped, {'token': secret_val})
        self.assertIsNone(input_handoff.pop_secret_values(handoff['id']))

        # Origin anti-phishing check
        self.assertIsNotNone(input_handoff.classify_origin_warning('http://192.168.1.50:8765'))
        self.assertIsNone(input_handoff.classify_origin_warning('https://rove.example.com'))

    def test_5_rich_ui_parser_and_color_governance(self):
        from workstation import rich_ui

        dsl = '\n'.join([
            '{"op":"create","id":"root","type":"card","title":"测试结果","backgroundColor":"#ff00ff"}',
            '{"op":"append","parent":"root","id":"s1","type":"stat","label":"通过","value":"100%","tone":"success","color":"#123456"}',
            '{"op":"append","parent":"root","id":"b1","type":"button","label":"继续","send":"请继续","tone":"neon"}',
        ])
        parsed = rich_ui.parse_rove_ui_lines(dsl)
        self.assertEqual(parsed['errors'], 0)
        self.assertNotIn('backgroundColor', parsed['nodes']['root']['props'])
        self.assertNotIn('color', parsed['nodes']['s1']['props'])
        self.assertEqual(parsed['nodes']['s1']['props']['tone'], 'success')
        self.assertEqual(parsed['nodes']['b1']['props']['tone'], 'primary')

    def test_6_memories_fts_search_consolidate_and_rollover(self):
        from workstation import context_bridge, memories

        m1 = memories.add_memory(self.project_id, '严禁硬编码十六进制色值', category='constraint', why='保持双主题一致')
        m2 = memories.add_memory(self.project_id, '按钮必须保留 focus-visible 状态', category='convention')
        hits = memories.search_memories(self.project_id, '十六进制')
        self.assertTrue(any(h['id'] == m1['id'] for h in hits))

        merged = memories.consolidate_memories(
            self.project_id,
            [m1['id'], m2['id']],
            '严禁硬编码十六进制色值且按钮必须保留 focus-visible',
            category='convention',
        )
        self.assertEqual(len(memories.list_memories(self.project_id)), 1)
        self.assertIn('focus-visible', merged['content'])

        self.assertTrue(context_bridge.is_context_overflow_error('maximum context length exceeded'))
        self.db.event(self.session_id, 'j_ov', 'user', {'text': '重构数据库层'})
        rollover = context_bridge.build_rollover_context(
            self.db.one('SELECT * FROM sessions WHERE id=?', (self.session_id,)),
            self.db.one('SELECT * FROM projects WHERE id=?', (self.project_id,)),
            'codex',
        )
        self.assertIn('上下文满窗自动换窗交接', rollover)

    def test_7_vision_bridge_and_design_audit(self):
        from workstation import design_guard, vision_bridge

        img = self.tmp / 'shot.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n')
        desc = vision_bridge.describe_images_for_text_model(
            [img],
            focus_hint='看下报错',
            bridge_cfg={'base_url': 'https://api.example.com/v1', 'api_key': 'k', 'model': 'gpt-4o'},
            describe_fn=lambda p, h, c: '页面顶部显示 502 Bad Gateway',
        )
        self.assertIn('502 Bad Gateway', desc)

        # Verify new/modified UI files (RichUiBlock.jsx, Conversation.jsx) have zero Anti-Slop or hex violations
        all_findings = design_guard.audit_frontend_directory(str(ROOT / 'frontend' / 'src'))
        new_ui_findings = [f for f in all_findings if f['file'] in ('RichUiBlock.jsx', 'Conversation.jsx')]
        self.assertEqual(new_ui_findings, [])

        # Verify audit_frontend_directory catches hardcoded hex in a synthetic component
        ui_tmp = self.tmp / 'ui'
        ui_tmp.mkdir()
        (ui_tmp / 'Bad.jsx').write_text('export const Bad = () => <div style={{color: "#ff00ff"}}/>;\n', encoding='utf-8')
        self.assertTrue(any(f['rule_id'] == 'hardcoded_hex_color' for f in design_guard.audit_frontend_directory(str(ui_tmp))))


if __name__ == '__main__':
    unittest.main()
