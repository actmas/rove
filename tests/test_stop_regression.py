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
