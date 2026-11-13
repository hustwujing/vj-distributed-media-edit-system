import sys
import unittest
from unittest.mock import patch

from tests import test_workflows as fixtures
from tests import campaign_dynamic_probe
with patch.dict(sys.modules,{'campaign_dynamic_probe':campaign_dynamic_probe}):
    from tests.campaign_dynamic_faults import interfering_runs
from webapp import workflow_graph as graph


class CampaignFaultIsolationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkflowTests();self.fixture.setUp();self.store=self.fixture.store
        self.authorization={'goal':'fixture','allowed_assets':['asset'],'allowed_operations':['script'],
            'allowed_agent_profiles':[],'delivery_targets':['report'],
            'content_fingerprint':graph.content_fingerprint(self.store,self.fixture.project)}

    def tearDown(self):self.fixture.tearDown()

    def create(self,name):
        project=self.store.add_project('owner',name)['id']
        asset_id='asset-'+name
        self.store.add_asset(project,asset_id,'asset.txt','asset.txt',{'kind':'text','size':1})
        authorization={**self.authorization,'allowed_assets':[asset_id],
            'content_fingerprint':graph.content_fingerprint(self.store,project)}
        return project,graph.create(self.store,project,None,authorization)

    def test_idle_waiting_user_run_is_quiescent_but_live_transition_is_not(self):
        owned_project,_=self.create('owned')
        other_project,other=self.create('other')
        self.store.execute("UPDATE runs SET status='WAITING_USER',terminal_reason='clarification_exhausted' WHERE id=?",(other['id'],))
        self.store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=?",(other['id'],))
        self.assertEqual(interfering_runs(self.store,owned_project),[])
        self.store.execute("UPDATE runs SET status='ACTIVE',terminal_reason='' WHERE id=?",(other['id'],))
        self.assertEqual(interfering_runs(self.store,owned_project),[{'id':other['id']}])
        self.store.execute("UPDATE runs SET status='WAITING_USER' WHERE id=?",(other['id'],))
        graph.command(self.store,other['id'],'owner','pause',request_id='4'*32)
        self.assertEqual(interfering_runs(self.store,owned_project),[{'id':other['id']}])
