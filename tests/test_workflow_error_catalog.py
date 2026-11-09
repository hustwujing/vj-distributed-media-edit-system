import unittest

from webapp.workflow_error_catalog import CATALOG,TASK_FAILURE_CODES,get,message,public_metadata
from webapp.workflow_repair import resolve


class WorkflowErrorCatalogTests(unittest.TestCase):
    def test_every_task_failure_has_explicit_public_and_recovery_semantics(self):
        expected={
            'submission_unknown','result_unavailable','poll_failed','collect_failed','provider_failed',
            'provider_cancelled','pre_submit','technical_validation_failed','quality_failed',
            'review_incomplete','review_contract_exhausted','planning_contract_exhausted','model_result',
            'agent_failed','agent_model_transport_failed','agent_timeout','agent_turn_budget_exhausted',
            'agent_finalize_budget_exhausted','agent_contract_exhausted',
            'agent_runtime_unavailable','runtime_profile_changed',
            'policy_changed','execution_failed','invalid_result_manifest','missing_proposal',
            'model_token_budget_exhausted','budget_exhausted','daily_model_quota_exhausted'}
        self.assertEqual(TASK_FAILURE_CODES,expected)
        for code in sorted(expected):
            with self.subTest(code=code):
                item=get(code);metadata=public_metadata(code);resolution=resolve(code)
                self.assertTrue(item.title and item.message and item.kind)
                self.assertNotIn('请确认下面列出的事项',item.message)
                self.assertEqual((resolution.action,resolution.reason),
                    (item.recovery_action,item.recovery_reason))
                self.assertTrue(metadata['known_error_code'])
                self.assertNotEqual(metadata['failure_kind'],'unknown')

    def test_safe_detail_is_shown_together_with_registered_guidance(self):
        text=message('agent_timeout','专业 Agent 本轮运行时限已到。')
        self.assertIn('运行时限已到',text)
        self.assertIn('不会自动重放',text)
        provider=message('provider_failed','GPU 显存不足，请降低质量后重试')
        self.assertIn('GPU 显存不足',provider)
        self.assertIn('不会自动重复提交',provider)

    def test_unknown_code_is_explicitly_unregistered(self):
        self.assertNotIn('unregistered_failure',CATALOG)
        metadata=public_metadata('unregistered_failure')
        self.assertFalse(metadata['known_error_code'])
        self.assertEqual(metadata['failure_kind'],'unknown')
        self.assertEqual(resolve('unregistered_failure').action,'stop')

    def test_run_level_delivery_manifest_error_is_registered_but_not_a_task_code(self):
        self.assertIn('invalid_delivery_manifest',CATALOG)
        self.assertNotIn('invalid_delivery_manifest',TASK_FAILURE_CODES)
        self.assertEqual(public_metadata('invalid_delivery_manifest')['failure_kind'],'contract')


if __name__=='__main__':unittest.main()
