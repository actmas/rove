import asyncio
import unittest
from unittest.mock import patch, Mock

from workstation import worker, engines


class StopRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_expired_antigravity_login_has_actionable_error(self):
        result = engines.classify_error('error getting token source: You are not logged into Antigravity.')
        self.assertEqual(result['code'], 'authentication_required')
        self.assertIn('登录', result['detail'])

    async def test_stop_cancels_stalled_startup_and_allows_next_turn(self):
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def stalled(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        with patch.object(worker, 'run_codex', stalled), patch.object(worker.db, 'one', return_value={'stop': 1}), patch.object(worker, 'time', Mock(monotonic=Mock(side_effect=[0, 0, 19]))):
            outcome = await worker.run_codex_stoppable({'id': 'job'}, {}, {})
        self.assertTrue(entered.is_set())
        self.assertTrue(closed.is_set())
        self.assertEqual(outcome['status'], 'stopped')

        async def completed(*args):
            return {'status': 'completed', 'error': None}

        with patch.object(worker, 'run_codex', completed), patch.object(worker.db, 'one', return_value={'stop': 0}):
            outcome = await worker.run_codex_stoppable({'id': 'next'}, {}, {})
        self.assertEqual(outcome['status'], 'completed')

    def test_antigravity_live_states_ignores_post_stop_and_stale_not_fully_idle(self):
        from datetime import datetime, timezone
        from pathlib import Path
        import sqlite3
        import tempfile
        import time
        from workstation import antigravity_sync

        now = time.time()
        fresh_iso = datetime.fromtimestamp(now - 5, timezone.utc).strftime('%Y-%m-%d %H:%M:%S+00:00')
        stale_iso = datetime.fromtimestamp(now - 300, timezone.utc).strftime('%Y-%m-%d %H:%M:%S+00:00')

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = sqlite3.connect(home / 'conversation_summaries.db')
            conn.execute('CREATE TABLE conversation_summaries(conversation_id TEXT, step_count INT, last_modified_time TEXT, not_fully_idle INT)')
            conn.execute("INSERT INTO conversation_summaries VALUES('c_stopped', 143, ?, 1)", (fresh_iso,))
            conn.execute("INSERT INTO conversation_summaries VALUES('c_stale', 50, ?, 1)", (stale_iso,))
            conn.execute("INSERT INTO conversation_summaries VALUES('c_live', 10, ?, 1)", (fresh_iso,))
            conn.commit()
            conn.close()

            sessions = [
                # Stopped 2s ago, marker changed by post-cancel step (134 -> 143): must NOT re-lock
                {'id': 's_stopped', 'native_antigravity_id': 'c_stopped', 'external_stopped_at': now - 2, 'external_stopped_marker': '134:old'},
                # Stale not_fully_idle (5m old): must NOT lock
                {'id': 's_stale', 'native_antigravity_id': 'c_stale', 'external_stopped_at': None, 'external_stopped_marker': ''},
                # Genuinely live external turn (5s ago, never stopped): should report active
                {'id': 's_live', 'native_antigravity_id': 'c_live', 'external_stopped_at': None, 'external_stopped_marker': ''},
            ]
            with patch.object(antigravity_sync, 'antigravity_home', return_value=home), patch.object(antigravity_sync.db, 'execute'):
                states = antigravity_sync.live_antigravity_states(sessions, set())
            self.assertFalse(states['s_stopped']['external_active'])
            self.assertFalse(states['s_stale']['external_active'])
            self.assertTrue(states['s_live']['external_active'])
