import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from workstation import antigravity_hub, engines, quotas


class ModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.configs = [
            {'modelId': f'gemini-4-hydrogen-{tier}', 'label': f'Hydrogen {tier}',
             'modelOrAlias': {'model': f'MODEL_HYDROGEN_{tier.upper()}'}}
            for tier in ('low', 'medium', 'high', 'xhigh')
        ]
        self.configs.append({
            'modelId': 'gemini-4-carbon-disabled', 'label': 'Carbon', 'disabled': True,
            'modelOrAlias': {'model': 'MODEL_CARBON'}, 'tagDescription': 'Requires Ultra',
        })

    def test_live_catalog_preserves_new_models_and_efforts(self):
        models = antigravity_hub.format_hub_models(self.configs)
        self.assertEqual(len(models), 4)
        self.assertEqual(sum(m['isDefault'] for m in models), 1)
        xhigh = next(m for m in models if m['id'].endswith('-xhigh'))
        self.assertEqual(xhigh['defaultEffort'], 'xhigh')
        self.assertEqual(xhigh['planModel'], 'MODEL_HYDROGEN_XHIGH')

    def test_model_resolution_preserves_xhigh_and_existing_high_fallback(self):
        resolve = antigravity_hub.resolve_plan_model
        self.assertEqual(resolve('gemini-4-hydrogen-xhigh', 'xhigh', self.configs), 'MODEL_HYDROGEN_XHIGH')
        self.assertEqual(resolve('gemini-4-hydrogen-high', 'xhigh', self.configs), 'MODEL_HYDROGEN_XHIGH')
        self.assertEqual(resolve('gemini-4-hydrogen-xhigh', 'medium', self.configs), 'MODEL_HYDROGEN_MEDIUM')
        self.assertEqual(resolve('gemini-4-argon-high', 'max', self.configs), 'MODEL_PLACEHOLDER_M417')
        with self.assertRaisesRegex(RuntimeError, 'Requires Ultra'):
            resolve('gemini-4-carbon-disabled', None, self.configs)

    def test_quota_metadata_refreshes_catalog_without_extra_model_request(self):
        hub = AsyncMock()
        hub.retrieve_user_quota_summary.return_value = {}
        hub.get_user_status.return_value = {
            'cascadeModelConfigData': {'clientModelConfigs': self.configs},
        }
        expected = antigravity_hub.format_hub_models(self.configs)
        with patch.object(antigravity_hub, 'discover_hub', AsyncMock(return_value=hub)), \
             patch.object(engines, 'engine_models', return_value={'models': []}), \
             patch.object(engines, 'save_models') as save, \
             patch.object(quotas.db, 'config', return_value={}):
            asyncio.run(quotas.get_antigravity_quotas())
            save.assert_called_once_with('antigravity', expected)
            hub.get_model_configs.assert_not_awaited()

    def test_empty_quota_metadata_preserves_existing_catalog(self):
        hub = AsyncMock()
        hub.retrieve_user_quota_summary.return_value = {}
        hub.get_user_status.return_value = {}
        with patch.object(antigravity_hub, 'discover_hub', AsyncMock(return_value=hub)), \
             patch.object(engines, 'save_models') as save, \
             patch.object(quotas.db, 'config', return_value={}):
            asyncio.run(quotas.get_antigravity_quotas())
            save.assert_not_called()
