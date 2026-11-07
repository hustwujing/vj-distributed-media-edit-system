import unittest
from dataclasses import replace
from types import SimpleNamespace
from webapp.agent_profiles import AgentProfile
from webapp.specialist_profiles import get
from webapp.workflow_agents import model_output_limit


class ModelOutputBudgetTests(unittest.TestCase):
    def test_model_policy_is_frozen_and_changes_configuration_identity(self):
        phase=AgentProfile();full=replace(phase,output_token_policy='profile')
        self.assertNotEqual(phase.digest,full.digest)
        self.assertEqual(AgentProfile(**full.snapshot()).output_token_policy,'profile')
        self.assertEqual(AgentProfile(**full.snapshot()).digest,full.digest)
        with self.assertRaises(ValueError):replace(phase,output_token_policy='unbounded')

    def test_role_phase_and_model_caps_remain_independent(self):
        planner=get('media_planner');draft=SimpleNamespace(schemas={'submit_typed_plan':{}},correction_mode=False)
        commit=SimpleNamespace(schemas={'commit_plan':{}},correction_mode=False)
        self.assertEqual(model_output_limit(planner,draft,policy='phase',maximum=16000),6144)
        self.assertEqual(model_output_limit(planner,draft,policy='profile',maximum=16000),12000)
        self.assertEqual(model_output_limit(planner,commit,policy='phase',maximum=16000),1024)
        self.assertEqual(model_output_limit(planner,commit,policy='profile',maximum=16000),12000)
        self.assertEqual(model_output_limit(planner,draft,policy='profile',maximum=5000),5000)
        self.assertEqual(model_output_limit(get('delivery_reviewer'),None,policy='profile',maximum=16000),8000)
        with self.assertRaises(ValueError):model_output_limit(planner,draft,policy='unbounded')
