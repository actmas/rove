import json
import unittest
from unittest.mock import AsyncMock, patch
from workstation import antigravity_hub as hub_module


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_mode_waits_for_answer_and_returns_option_ids(self):
        hub = AsyncMock()
        step = {'askQuestion': {'questions': [
            {'question': 'Choose', 'options': [{'id': 'a', 'text': 'First'}]}
        ]}}

        def one(query, args):
            if 'stop' in query:
                return {'stop': 0}
            return {'decision': json.dumps({'answers': {'0': {'answers': ['a']}}})}

        with patch.object(hub_module.db, 'one', side_effect=one), patch.object(hub_module.db, 'execute'), patch.object(hub_module.db, 'event') as event:
            await hub_module._handle_waiting_step(hub, 'cascade', 'trajectory', 2, step, 'auto', {'id': 'job'}, {'id': 'session'})
        self.assertEqual(event.call_args.args[3]['method'], 'item/tool/requestUserInput')
        interaction = hub.call.call_args.args[1]['interaction']
        self.assertEqual(interaction['askQuestion']['questions'][0]['selectedOptionIds'], ['a'])
        self.assertNotIn('permission', interaction)
