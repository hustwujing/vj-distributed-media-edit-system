import copy
import unittest
from tests.campaign_model_fixture import digest,facts_digest,rebind,validate


class ModelEvaluationFixtureTests(unittest.TestCase):
    def fixture(self):
        frozen={'goal':'Keep original subject','authorization':{'allowed_agent_profiles':['implementer'],
            'agent_profile_digests':{'implementer':'old'},'allowed_assets':['asset-original'],'model_token_budget':8000},
            'tasks':[{'id':'preserve','revision':3,'acceptance':['unchanged']}],
            'input_batch':{'through_sequence':7}}
        frozen['snapshot_digest']=digest(frozen)
        return {'cases':[{'id':'repair','frozen':frozen,'archive_run':{'id':'immutable'}}]}

    def test_stale_policy_is_rejected_before_calls(self):
        with self.assertRaisesRegex(ValueError,'policy binding'):validate(self.fixture(),lambda _: 'new')

    def test_explicit_rebind_changes_only_policy_binding_and_request_digest(self):
        original=self.fixture();backup=copy.deepcopy(original)
        rebound=rebind(original,lambda _: 'new')
        self.assertEqual(original,backup)
        self.assertEqual(facts_digest(original),facts_digest(rebound))
        self.assertNotEqual(original['cases'][0]['frozen']['snapshot_digest'],rebound['cases'][0]['frozen']['snapshot_digest'])
        validate(rebound,lambda _: 'new')
        rebound['cases'][0]['frozen']['authorization']['allowed_assets'].append('unapproved')
        self.assertNotEqual(facts_digest(original),facts_digest(rebound))
        with self.assertRaisesRegex(ValueError,'snapshot digest'):validate(rebound,lambda _: 'new')

    def test_budget_goal_and_acceptance_remain_part_of_facts_identity(self):
        original=self.fixture()
        for field,value in [('goal','changed'),('tasks',[])]:
            changed=copy.deepcopy(original);changed['cases'][0]['frozen'][field]=value
            self.assertNotEqual(facts_digest(original),facts_digest(changed))
        changed=copy.deepcopy(original);changed['cases'][0]['frozen']['authorization']['model_token_budget']=9000
        self.assertNotEqual(facts_digest(original),facts_digest(changed))
