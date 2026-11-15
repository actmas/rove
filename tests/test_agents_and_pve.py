import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest


class TestUniversalAgentsAndPveRecovery(unittest.TestCase):
    def setUp(self):
        from workstation import store as db
        self._orig_data = db.DATA
        self._orig_env = os.environ.get('WORKSTATION_DATA')
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name) / 'data'
        self.ws_dir = Path(self.tmp_dir.name) / 'workspaces'
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        os.environ['WORKSTATION_DATA'] = str(self.data_dir)

        db.DATA = self.data_dir
        db.init()
        db.execute('DELETE FROM projects')
        cfg = db.config()
        cfg['allowed_roots'] = [str(self.ws_dir)]
        db.write_private(self.data_dir / 'config.json', json.dumps(cfg))

    def tearDown(self):
        from workstation import store as db
        db.DATA = self._orig_data
        if self._orig_env is None:
            os.environ.pop('WORKSTATION_DATA', None)
        else:
            os.environ['WORKSTATION_DATA'] = self._orig_env
        self.tmp_dir.cleanup()

    def test_custom_agent_registration_and_model_resolution(self):
        from workstation import agents, engines

        saved = agents.upsert_custom_agent({
            'id': 'claude-code',
            'name': 'Claude Code',
            'adapter': 'cli',
            'binary': 'claude',
            'models': ['claude-sonnet-4-6', 'claude-opus-4-6'],
        })
        self.assertEqual(saved['id'], 'claude-code')
        self.assertTrue(agents.is_valid_engine('claude-code'))
        self.assertTrue(agents.is_valid_engine('codex'))
        self.assertFalse(agents.is_valid_engine('nonexistent-engine'))

        cat = engines.catalog()
        self.assertIn('claude-code', cat)
        self.assertEqual(cat['claude-code']['models'][0]['id'], 'claude-sonnet-4-6')

        # Foreign model from codex ('gpt-6.1-sol') should automatically resolve to claude-code's default
        resolved_model, resolved_effort = engines.resolve_model('claude-code', 'gpt-6.1-sol', 'medium')
        self.assertEqual(resolved_model, 'claude-sonnet-4-6')

        # Switching back to codex from claude-sonnet-4-6 should resolve to codex's default
        codex_model, _ = engines.resolve_model('codex', 'claude-sonnet-4-6', 'medium')
        self.assertEqual(codex_model, 'gpt-6.1-sol')

        # Deleting custom agent removes it from catalog
        self.assertTrue(agents.delete_custom_agent('claude-code'))
        self.assertFalse(agents.is_valid_engine('claude-code'))

    def test_multi_agent_context_handoff_and_cli_execution(self):
        from workstation import agents, context_bridge, store as db

        # Create a mock CLI agent executable script
        bin_dir = Path(self.tmp_dir.name) / 'bin'
        bin_dir.mkdir(exist_ok=True)
        mock_cli = bin_dir / 'mock-agent'
        mock_cli.write_text(
            '#!/bin/sh\n'
            'echo \'{"type":"assistant","session_id":"mock_sess_99","message":{"content":[{"type":"thinking","thinking":"Analyzing handoff context"},{"type":"text","text":"Handled by Mock Agent successfully."}]}}\'\n',
            encoding='utf-8',
        )
        os.chmod(mock_cli, 0o755)

        agents.upsert_custom_agent({
            'id': 'mock-agent',
            'name': 'Mock Agent',
            'adapter': 'cli',
            'binary': str(mock_cli),
            'args_template': ['-p', '{prompt}'],
            'output_format': 'claude-stream-json',
            'models': ['mock-v1'],
        })

        proj_path = self.ws_dir / 'demo-proj'
        proj_path.mkdir(parents=True, exist_ok=True)
        db.execute('INSERT INTO projects(id,name,path) VALUES(?,?,?)', ('p1', 'demo-proj', str(proj_path)))
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id,project_id,engine,title,created,updated,last_engine) VALUES(?,?,?,?,?,?,?)',
            ('s1', 'p1', 'codex', 'Multi-Agent Test', now, now, 'codex'),
        )
        # Simulate Turn 1 completed by Codex
        db.event('s1', 'j1', 'user', {'text': 'Design the initial API schema', 'engine': 'codex'})
        db.event('s1', 'j1', 'codex', {
            'method': 'item/completed',
            'params': {'item': {'id': 'm1', 'type': 'agentMessage', 'text': 'Initial schema created in schema.py.'}},
        })

        sess = db.one('SELECT * FROM sessions WHERE id=?', ('s1',))
        proj = db.one('SELECT * FROM projects WHERE id=?', ('p1',))

        # Switching to mock-agent should trigger cross-engine handoff context synthesis
        handoff = context_bridge.build_handoff_context(sess, proj, target_engine='mock-agent', exclude_job_id='j2')
        self.assertIn('CODEX` ➔ `MOCK-AGENT`', handoff)
        self.assertIn('Design the initial API schema', handoff)

        # Run turn with mock-agent
        job = {'id': 'j2', 'session_id': 's1', 'prompt': 'Now add validation rules', 'model': 'mock-v1', 'mode': 'standard'}
        db.execute(
            'INSERT INTO jobs(id,session_id,request_id,prompt,status,engine,model) VALUES(?,?,?,?,?,?,?)',
            ('j2', 's1', 'req_j2', job['prompt'], 'running', 'mock-agent', 'mock-v1'),
        )
        outcome = asyncio.run(agents.run_custom_agent('mock-agent', job, sess, proj, []))
        self.assertEqual(outcome['status'], 'completed')

        updated_sess = db.one('SELECT * FROM sessions WHERE id=?', ('s1',))
        self.assertEqual(agents.get_session_native_agent_id(updated_sess, 'mock-agent'), 'mock_sess_99')

        # Switching from mock-agent to antigravity should now synthesize handoff from MOCK-AGENT -> ANTIGRAVITY
        handoff_to_agy = context_bridge.build_handoff_context(updated_sess, proj, target_engine='antigravity', exclude_job_id='j3')
        self.assertIn('MOCK-AGENT` ➔ `ANTIGRAVITY`', handoff_to_agy)
        self.assertIn('Handled by Mock Agent successfully.', handoff_to_agy)

    def test_snapshot_self_healing_and_pve_cross_platform_remap(self):
        from workstation import cluster_sync, store as db

        # 1. Create a project with source files
        proj_dir = self.ws_dir / 'critical-app'
        proj_dir.mkdir(parents=True, exist_ok=True)
        main_py = proj_dir / 'main.py'
        readme_md = proj_dir / 'README.md'
        main_py.write_text('print("v1 original")\n', encoding='utf-8')
        readme_md.write_text('# Critical App\nDo not lose this file!\n', encoding='utf-8')

        db.execute('INSERT INTO projects(id,name,path) VALUES(?,?,?)', ('proj_crit', 'critical-app', str(proj_dir.resolve())))
        now = time.time()
        db.execute(
            'INSERT INTO sessions(id,project_id,engine,title,created,updated,cwd) VALUES(?,?,?,?,?,?,?)',
            ('sess_crit', 'proj_crit', 'codex', 'Critical Session', now, now, str(proj_dir.resolve())),
        )

        # 2. Create full self-healing snapshot
        snap_info = cluster_sync.create_snapshot(include_projects=True, label='test-backup', include_template=False)
        snap_path = Path(snap_info['path'])
        self.assertTrue(snap_path.is_file())

        # 3. Simulate accidental deletion of README.md while main.py has newer edits
        readme_md.unlink()
        main_py.write_text('print("v2 uncommitted edit")\n', encoding='utf-8')
        self.assertFalse(readme_md.exists())

        # Run self-healing (only_missing_files=True): restores deleted README.md without overwriting v2 main.py!
        heal_res = cluster_sync.restore_snapshot(
            snap_path,
            restore_db=False,
            restore_projects=True,
            only_missing_files=True,
        )
        self.assertTrue(heal_res['ok'])
        self.assertTrue(readme_md.is_file())
        self.assertEqual(readme_md.read_text(encoding='utf-8'), '# Critical App\nDo not lose this file!\n')
        self.assertEqual(main_py.read_text(encoding='utf-8'), 'print("v2 uncommitted edit")\n')

        # 4. Simulate restoring snapshot onto a PVE Linux node with a different workspace root
        pve_root = Path(self.tmp_dir.name) / 'pve_var_lib_workspaces'
        pve_root.mkdir(parents=True, exist_ok=True)
        cfg = db.config()
        cfg['allowed_roots'] = [str(pve_root.resolve())]
        db.write_private(self.data_dir / 'config.json', json.dumps(cfg))
        shutil.rmtree(proj_dir)

        pve_res = cluster_sync.restore_snapshot(
            snap_path,
            restore_db=True,
            restore_projects=True,
            only_missing_files=False,
            target_workspace_root=str(pve_root.resolve()),
        )
        self.assertTrue(pve_res['ok'])
        self.assertEqual(pve_res['remapped_paths_count'], 1)
        remapped_proj = db.one('SELECT * FROM projects WHERE id=?', ('proj_crit',))
        self.assertTrue(Path(remapped_proj['path']).is_relative_to(pve_root.resolve()))
        self.assertTrue((Path(remapped_proj['path']) / 'main.py').is_file())

    def test_pve_dual_disk_snapshot_redundancy_and_fallback(self):
        from unittest.mock import patch, MagicMock
        from workstation import cluster_sync

        # 1. Test configuration includes pve_secondary_backup_dir
        cfg = cluster_sync.get_cluster_config()
        self.assertIn('pve_secondary_backup_dir', cfg)
        self.assertEqual(cfg['pve_secondary_backup_dir'], '/mnt/pve/backup/walking-workstation-snapshots')

        sec_dir = Path(self.tmp_dir.name) / 'pve_secondary_disk'
        cluster_sync.update_cluster_config({'pve_secondary_backup_dir': str(sec_dir)})
        self.assertEqual(cluster_sync.get_cluster_config()['pve_secondary_backup_dir'], str(sec_dir))

        # 2. Test local mirror_snapshot_to_secondary
        snap_info = cluster_sync.create_snapshot(include_projects=False, label='test-mirror')
        snap_path = Path(snap_info['path'])
        mirrored = cluster_sync.mirror_snapshot_to_secondary(snap_path)
        self.assertIsNotNone(mirrored)
        self.assertTrue(mirrored.is_file())
        self.assertEqual(mirrored.stat().st_size, snap_path.stat().st_size)
        self.assertTrue(mirrored.is_relative_to(sec_dir))

        # 3. Test push_snapshot_to_peer generates dual-disk copy command
        cluster_sync.update_cluster_config({
            'pve_ssh_host': 'mock-pve',
            'peer_url': '',
            'remote_data_dir': '/var/lib/walking-workstation',
            'pve_secondary_backup_dir': '/mnt/pve/backup/walking-workstation-snapshots',
        })

        captured_commands = []
        def mock_run(cmd, **kwargs):
            captured_commands.append(cmd)
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = b''
            mock_res.stderr = b''
            return mock_res

        with patch('subprocess.run', side_effect=mock_run):
            push_res = cluster_sync.push_snapshot_to_peer(include_projects=False)
            self.assertTrue(push_res['ok'])
            self.assertEqual(push_res['transport'], 'ssh')
            self.assertIn('双盘同步', push_res['detail'])
            self.assertEqual(push_res['secondary_path'], f"/mnt/pve/backup/walking-workstation-snapshots/{push_res['snapshot']['filename']}")

            ssh_exec_args = [c for c in captured_commands if 'ssh' in c[0]]
            self.assertTrue(len(ssh_exec_args) >= 1)
            self.assertIn('--', ssh_exec_args[0])
            full_cmd_str = ' '.join(ssh_exec_args[0])
            self.assertIn('/var/lib/walking-workstation/snapshots', full_cmd_str)
            self.assertIn('/mnt/pve/backup/walking-workstation-snapshots', full_cmd_str)
            self.assertIn('cp -f', full_cmd_str)

        # 4. Test pull_and_restore_from_peer fallback logic in fetch script
        captured_pull_commands = []
        def mock_pull_run(cmd, **kwargs):
            captured_pull_commands.append(cmd)
            out_file = kwargs.get('stdout')
            if out_file and hasattr(out_file, 'write'):
                with open(snap_path, 'rb') as f_src:
                    out_file.write(f_src.read())
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stderr = b''
            return mock_res

        with patch('subprocess.run', side_effect=mock_pull_run):
            pull_res = cluster_sync.pull_and_restore_from_peer(restore_db=False, restore_projects=False)
            self.assertTrue(pull_res['ok'])
            self.assertTrue(len(captured_pull_commands) >= 1)
            self.assertIn('--', captured_pull_commands[0])
            fetch_cmd_str = ' '.join(captured_pull_commands[0])
            self.assertIn('/var/lib/walking-workstation/snapshots', fetch_cmd_str)
            self.assertIn('/mnt/pve/backup/walking-workstation-snapshots', fetch_cmd_str)

    def test_security_validations_ssh_and_urls(self):
        from workstation import cluster_sync, agents

        # SSH Host validation
        self.assertEqual(cluster_sync.validate_ssh_host('root@192.168.1.100'), 'root@192.168.1.100')
        self.assertEqual(cluster_sync.validate_ssh_host('pve-node-1'), 'pve-node-1')
        with self.assertRaises(ValueError):
            cluster_sync.validate_ssh_host('-oProxyCommand=id')
        with self.assertRaises(ValueError):
            cluster_sync.validate_ssh_host('host; rm -rf /')

        # Peer URL validation
        self.assertEqual(cluster_sync.validate_peer_url('http://192.168.1.100:8765'), 'http://192.168.1.100:8765')
        self.assertEqual(cluster_sync.validate_peer_url('https://pve.local/'), 'https://pve.local')
        with self.assertRaises(ValueError):
            cluster_sync.validate_peer_url('file:///etc/passwd')
        with self.assertRaises(ValueError):
            cluster_sync.validate_peer_url('gopher://127.0.0.1')

        # Agent base_url validation
        self.assertEqual(agents.validate_agent_base_url('https://api.deepseek.com/v1'), 'https://api.deepseek.com/v1')
        with self.assertRaises(ValueError):
            agents.validate_agent_base_url('file:///etc/shadow')
        with self.assertRaises(ValueError):
            agents.validate_agent_base_url('')


if __name__ == '__main__':
    unittest.main()
