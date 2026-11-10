import unittest
from dataclasses import replace
from types import SimpleNamespace
from webapp.agent_profiles import AgentProfile
from webapp.specialist_profiles import get
from webapp.workflow_agents import model_output_limit


class ModelOutputBudgetTests(unittest.TestCase):
    def test_strict_tool_policy_is_frozen_and_typed(self):
        profile=AgentProfile(strict_tools=False)
        self.assertFalse(AgentProfile(**profile.snapshot()).strict_tools)
        with self.assertRaises(ValueError):AgentProfile(strict_tools='false')

    def test_model_policy_is_frozen_and_changes_configuration_identity(self):
        phase=AgentProfile();full=replace(phase,output_token_policy='profile')
        self.assertNotEqual(phase.digest,full.digest)
        self.assertEqual(AgentProfile(**full.snapshot()).output_token_policy,'profile')
        self.assertEqual(AgentProfile(**full.snapshot()).digest,full.digest)
        with self.assertRaises(ValueError):replace(phase,output_token_policy='unbounded')
        overridden=replace(full,specialist_output_tokens={'media_planner':16000})
        self.assertNotEqual(full.digest,overridden.digest)
        self.assertEqual(AgentProfile(**overridden.snapshot()).specialist_output_tokens,{'media_planner':16000})
        for limits in ({'unknown_role':16000},{'media_planner':True},{'media_planner':0}):
            with self.assertRaises(ValueError):replace(full,specialist_output_tokens=limits)
        timed=replace(full,specialist_minimum_seconds={'editing_implementer':600})
        self.assertEqual(AgentProfile(**timed.snapshot()).specialist_minimum_seconds,{'editing_implementer':600})
        for limits in ({'unknown_role':600},{'editing_implementer':True},{'editing_implementer':9},
                       {'editing_implementer':timed.budget['run_seconds']+1}):
            with self.assertRaises(ValueError):replace(timed,specialist_minimum_seconds=limits)
        reasoned=replace(full,specialist_reasoning_effort={'editing_implementer':'medium'})
        self.assertEqual(AgentProfile(**reasoned.snapshot()).specialist_reasoning_effort,
            {'editing_implementer':'medium'})
        for efforts in ({'unknown_role':'medium'},{'editing_implementer':'unbounded'},
                        {'editing_implementer':True}):
            with self.assertRaises(ValueError):replace(reasoned,specialist_reasoning_effort=efforts)
        deepseek=AgentProfile(harness='deepseek_harness',protocol='anthropic',reasoning_effort='high')
        with self.assertRaises(ValueError):replace(deepseek,specialist_reasoning_effort={'editing_implementer':'medium'})

    def test_role_phase_and_model_caps_remain_independent(self):
        planner=get('media_planner');draft=SimpleNamespace(schemas={'submit_typed_plan':{}},correction_mode=False)
        commit=SimpleNamespace(schemas={'commit_plan':{}},correction_mode=False)
        self.assertEqual(model_output_limit(planner,draft,policy='phase',maximum=16000),6144)
        self.assertEqual(model_output_limit(planner,draft,policy='profile',maximum=16000),12000)
        self.assertEqual(model_output_limit(planner,commit,policy='phase',maximum=16000),1024)
        self.assertEqual(model_output_limit(planner,commit,policy='profile',maximum=16000),12000)
        self.assertEqual(model_output_limit(planner,draft,policy='profile',maximum=5000),5000)
        self.assertEqual(model_output_limit(planner,draft,policy='profile',maximum=16000,role_maximum=20000),16000)
        self.assertEqual(model_output_limit(get('delivery_reviewer'),None,policy='profile',maximum=16000),8000)
        with self.assertRaises(ValueError):model_output_limit(planner,draft,policy='unbounded')
