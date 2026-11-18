import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstation import agents
from workstation import model_router
from workstation import orchestration_schema as schema
from workstation import orchestration_store as ostore
from workstation import orchestrator
from workstation import store as db


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.old_data = db.DATA
        db.DATA = Path(self.tmpdir.name) / 'data'
        db.DATA.mkdir(parents=True, exist_ok=True)
        db._config_cache = None
        db.close_all_connections()
        db.init()

        self.proj_dir = Path(self.tmpdir.name) / 'proj'
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        (self.proj_dir / 'app.py').write_text('print("ok")\n', encoding='utf-8')

        self.project_id = 'proj_test_1'
        self.session_id = 'sess_test_1'
        db.execute(
            'INSERT INTO projects(id, name, path) VALUES(?,?,?)',
            (self.project_id, 'TestProj', str(self.proj_dir)),
        )
        db.execute(
            'INSERT INTO sessions(id, project_id, engine, title, created, updated, cwd) VALUES(?,?,?,?,?,?,?)',
            (self.session_id, self.project_id, 'codex', 'Main Session', 1.0, 1.0, str(self.proj_dir)),
        )
        self.project = db.one('SELECT * FROM projects WHERE id=?', (self.project_id,))
        self.session = db.one('SELECT * FROM sessions WHERE id=?', (self.session_id,))

    def tearDown(self):
        db.close_all_connections()
        db.DATA = self.old_data
        db._config_cache = None

    def test_plan_protocol_validation_rejects_bad_inputs(self):
        # Missing block
        parsed, err = schema.extract_plan_block('no plan here')
        self.assertIsNone(parsed)
        self.assertIsNotNone(err)

        # Duplicate blocks
        dup = (
            '<!-- ROVE_PLAN_START -->{"schema_version":1,"steps":[]}<!-- ROVE_PLAN_END -->\n'
            '<!-- ROVE_PLAN_START -->{"schema_version":1,"steps":[]}<!-- ROVE_PLAN_END -->'
        )
        parsed, err = schema.extract_plan_block(dup)
        self.assertIsNone(parsed)
        self.assertIn('2', err)

        # Forbidden permission field
        with self.assertRaises(schema.PlanValidationError):
            schema.validate_orchestration_plan({
                'schema_version': 1,
                'strategy': 'relay',
                'steps': [{
                    'id': 's1',
                    'role': 'coder_backend',
                    'mode': 'full',
                    'objective': 'hack',
                }],
            }, str(self.proj_dir))

        # Path traversal
        with self.assertRaises(schema.PlanValidationError):
            schema.validate_orchestration_plan({
                'schema_version': 1,
                'strategy': 'relay',
                'steps': [{
                    'id': 's1',
                    'role': 'coder_backend',
                    'files_scope': ['../secret.txt'],
                    'objective': 'escape',
                }],
            }, str(self.proj_dir))

        # Cycle detection
        with self.assertRaises(schema.PlanValidationError):
            schema.validate_orchestration_plan({
                'schema_version': 1,
                'strategy': 'relay',
                'steps': [
                    {'id': 'a', 'role': 'coder_backend', 'depends_on': ['b'], 'objective': 'step a'},
                    {'id': 'b', 'role': 'coder_backend', 'depends_on': ['a'], 'objective': 'step b'},
                ],
            }, str(self.proj_dir))

    def test_capability_matrix_and_heterogeneous_reviewer_routing(self):
        http_caps = agents.get_engine_capabilities('openai-http')
        self.assertFalse(http_caps['repo_write'])

        # Mock catalog with codex, antigravity, and openai-http
        fake_catalog = {
            'codex': {
                'models': [
                    {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1-Sol', 'isDefault': True, 'defaultEffort': 'medium', 'efforts': ['low', 'medium', 'high']},
                ],
            },
            'antigravity': {
                'models': [
                    {'id': 'gemini-4-argon-high', 'name': 'Gemini 4 Argon', 'isDefault': True, 'defaultEffort': 'high', 'efforts': ['low', 'medium', 'high']},
                ],
            },
        }
        with mock.patch.object(agents, 'get_engine_capabilities', side_effect=lambda e: {
            'codex': {'repo_read': True, 'repo_write': True, 'read_only_enforced': True, 'vision': True},
            'antigravity': {'repo_read': True, 'repo_write': True, 'read_only_enforced': True, 'vision': True},
            'openai-http': {'repo_read': False, 'repo_write': False, 'read_only_enforced': True, 'vision': False},
        }[e]):
            # Reviewer excluding openai_gpt should choose antigravity (gemini)
            rev = model_router.select_model_for_role(
                'reviewer',
                catalog=fake_catalog,
                installed_engines=['codex', 'antigravity', 'openai-http'],
                exclude_engine='codex',
                exclude_model_family='openai_gpt',
            )
            self.assertEqual(rev['engine'], 'antigravity')
            self.assertTrue(rev['heterogeneous'])

            # If only codex is available, reviewer degrades explicitly with degraded_reason
            rev_deg = model_router.select_model_for_role(
                'reviewer',
                catalog=fake_catalog,
                installed_engines=['codex'],
                exclude_engine='codex',
                exclude_model_family='openai_gpt',
            )
            self.assertEqual(rev_deg['engine'], 'codex')
            self.assertFalse(rev_deg['heterogeneous'])
            self.assertTrue(bool(rev_deg['degraded_reason']))

    def test_serial_relay_end_to_end_with_bounded_fix_and_idempotency(self):
        spawned_jobs = []

        def fake_spawn(dispatch_info):
            spawned_jobs.append(dispatch_info)
            ostore.mark_dispatch_spawned(dispatch_info['attempt_id'], dispatch_info['generation'])

        fake_route = {
            'engine': 'codex',
            'model': 'gpt-6.1-sol',
            'effort': 'medium',
            'capabilities': {'repo_read': True, 'repo_write': True, 'read_only_enforced': True},
            'reason': 'test route',
        }

        with mock.patch.object(orchestrator, 'spawn_dispatch_worker', side_effect=fake_spawn), \
             mock.patch.object(model_router, 'select_model_for_role', return_value=fake_route):
            run1 = orchestrator.start_orchestration(
                self.session,
                self.project,
                '实现功能并验证',
                strategy='relay',
                request_id='req_idempotent_1',
            )
            # Duplicate request_id returns same run without spawning another planner
            run2 = orchestrator.start_orchestration(
                self.session,
                self.project,
                '实现功能并验证',
                strategy='relay',
                request_id='req_idempotent_1',
            )
            self.assertEqual(run1['id'], run2['id'])
            self.assertEqual(len(spawned_jobs), 1)

            # 1. Planner finishes with valid ROVE_PLAN block
            plan_disp = spawned_jobs[-1]
            plan_job = db.one('SELECT * FROM jobs WHERE id=?', (plan_disp['job_id'],))
            plan_sess = db.one('SELECT * FROM sessions WHERE id=?', (plan_disp['sub_session_id'],))
            plan_reply = (
                '规划完成。\n<!-- ROVE_PLAN_START -->\n'
                + json.dumps({
                    'schema_version': 1,
                    'strategy': 'relay',
                    'steps': [
                        {'id': 's1', 'title': '编码实现', 'role': 'coder_backend', 'files_scope': ['app.py'], 'objective': '修改 app.py'},
                    ],
                })
                + '\n<!-- ROVE_PLAN_END -->'
            )
            with mock.patch.object(orchestrator, '_extract_job_reply', return_value=plan_reply):
                orchestrator.on_worker_finished(plan_job, plan_sess, self.project, {'status': 'completed', 'error': None})
                # Duplicate completion callback must be a no-op
                orchestrator.on_worker_finished(plan_job, plan_sess, self.project, {'status': 'completed', 'error': None})

            self.assertEqual(len(spawned_jobs), 2)
            coder_disp = spawned_jobs[-1]
            coder_job = db.one('SELECT * FROM jobs WHERE id=?', (coder_disp['job_id'],))
            coder_sess = db.one('SELECT * FROM sessions WHERE id=?', (coder_disp['sub_session_id'],))

            # 2. Coder finishes -> triggers cross-reviewer
            with mock.patch.object(orchestrator, '_extract_job_reply', return_value='已修改 app.py'):
                orchestrator.on_worker_finished(coder_job, coder_sess, self.project, {'status': 'completed', 'error': None})

            self.assertEqual(len(spawned_jobs), 3)
            rev_disp = spawned_jobs[-1]
            rev_job = db.one('SELECT * FROM jobs WHERE id=?', (rev_disp['job_id'],))
            rev_sess = db.one('SELECT * FROM sessions WHERE id=?', (rev_disp['sub_session_id'],))

            # 3. Reviewer finds 1 issue -> triggers auto-fix round 1
            with mock.patch.object(orchestrator, '_extract_job_reply', return_value='[P1] app.py:1 — 缺少错误处理\nFINDINGS_COUNT: 1'):
                orchestrator.on_worker_finished(rev_job, rev_sess, self.project, {'status': 'completed', 'error': None})

            self.assertEqual(len(spawned_jobs), 4)
            fix_disp = spawned_jobs[-1]
            fix_job = db.one('SELECT * FROM jobs WHERE id=?', (fix_disp['job_id'],))
            fix_sess = db.one('SELECT * FROM sessions WHERE id=?', (fix_disp['sub_session_id'],))

            # 4. Fix completes -> triggers re-review
            with mock.patch.object(orchestrator, '_extract_job_reply', return_value='已修复'):
                orchestrator.on_worker_finished(fix_job, fix_sess, self.project, {'status': 'completed', 'error': None})

            self.assertEqual(len(spawned_jobs), 5)
            rev2_disp = spawned_jobs[-1]
            rev2_job = db.one('SELECT * FROM jobs WHERE id=?', (rev2_disp['job_id'],))
            rev2_sess = db.one('SELECT * FROM sessions WHERE id=?', (rev2_disp['sub_session_id'],))

            # 5. Re-review passes with FINDINGS_COUNT: 0 -> Run completed!
            with mock.patch.object(orchestrator, '_extract_job_reply', return_value='复核通过\nFINDINGS_COUNT: 0'):
                orchestrator.on_worker_finished(rev2_job, rev2_sess, self.project, {'status': 'completed', 'error': None})

            final_run = ostore.get_run(run1['id'])
            self.assertEqual(final_run['status'], 'completed')
            self.assertEqual(final_run['fix_rounds'], 1)

    def test_cancel_prevents_subsequent_dispatch_and_recovery_skips_cancelled(self):
        spawned_jobs = []
        fake_route = {
            'engine': 'codex',
            'model': 'gpt-6.1-sol',
            'effort': 'medium',
            'capabilities': {'repo_read': True, 'repo_write': True, 'read_only_enforced': True},
            'reason': 'test route',
        }
        with mock.patch.object(orchestrator, 'spawn_dispatch_worker', side_effect=lambda d: spawned_jobs.append(d)), \
             mock.patch.object(model_router, 'select_model_for_role', return_value=fake_route):
            run = orchestrator.start_orchestration(
                self.session, self.project, '测试取消', strategy='relay', request_id='req_cancel_1',
            )
            self.assertEqual(len(spawned_jobs), 1)
            res = orchestrator.handle_run_action(run['id'], 'cancel', expected_revision=run['revision'])
            self.assertEqual(res['status'], 'cancelled')
            # Recovery scan must NOT respawn cancelled run's dispatch
            self.assertEqual(orchestrator.recover_pending_dispatches(), 0)


if __name__ == '__main__':
    unittest.main()
