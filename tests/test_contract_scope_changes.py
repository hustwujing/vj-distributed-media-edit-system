import unittest

from webapp.workflow_graph import Authorization


class ContractScopeChangeTests(unittest.TestCase):
    def base(self, **updates):
        value={
            'goal':'Create a media result',
            'allowed_assets':[],
            'allowed_operations':[],
            'provider_call_budget':{},
            'delivery_targets':['成片 MP4'],
            'content_fingerprint':'a'*64,
        }
        value.update(updates)
        return value

    def test_initial_scope_may_defer_delivery_targets_to_planner(self):
        authorization=Authorization.model_validate(self.base(delivery_targets=[]))
        self.assertEqual(authorization.delivery_targets,[])

    def test_scope_change_keeps_other_authorization_fields(self):
        authorization=Authorization.model_validate(self.base(
            delivery_targets=['成片 MP4'], allowed_assets=['asset'], model_token_budget=1024))
        narrowed=authorization.model_copy(update={'delivery_targets':['成片 MP4']})
        self.assertEqual(narrowed.allowed_assets,['asset'])
        self.assertEqual(narrowed.model_token_budget,1024)
