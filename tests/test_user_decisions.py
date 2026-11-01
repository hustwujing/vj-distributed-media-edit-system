import unittest
from tests import test_workflows as fixtures
local_step=fixtures.local_step
from webapp import workflows,user_decisions,workflow_review

class UserDecisionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkflowTests();self.f.setUp();self.f.submit([local_step()]);self.f.tick()
        workflows.state(self.f.store,self.f.turn,'paused')
        w=self.f.get();self.w=w;self.s=w['steps'][0]
        workflows.change(self.f.store,w['id'],self.s['id'],status='awaiting_review',candidate=self.s['result'],result=[])
    def tearDown(self):self.f.tearDown()
    def test_clarification_cannot_be_replaced_by_quality_approval(self):
        identifier=user_decisions.create(self.f.store,self.w,self.s,{'kind':'selection','question':'选择画幅','options':['横屏','竖屏']})
        with self.assertRaises(ValueError):workflows.quality_action(self.f.store,self.f.turn,self.s['id'],0,'approve','owner')
        with self.assertRaises(ValueError):user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'answer','value':'其他'},'owner')
        user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'answer','value':'横屏'},'owner')
        self.assertEqual(self.f.get()['agent_review']['purpose'],'diagnose')
        with self.assertRaises(ValueError):user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'answer','value':'横屏'},'owner')
    def test_external_approval_requires_explicit_proposal(self):
        identifier=user_decisions.create(self.f.store,self.w,self.s,{'kind':'external_call','question':'再生成一次？'})
        with self.assertRaises(ValueError):user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'approve'},'owner')
    def test_cancel_closes_pending_decisions(self):
        identifier=user_decisions.create(self.f.store,self.w,self.s,{'kind':'quality','question':'确认效果'})
        workflows.cancel(self.f.store,self.f.turn)
        self.assertFalse(user_decisions.pending(self.f.store,self.w['id']))
        with self.assertRaises(ValueError):user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'approve'},'owner')

    def test_candidate_selection_binds_output_role_without_regeneration(self):
        from webapp.operations import for_image
        image_spec=workflows.Step(id='package',name='image',kind='operation',operation='image.edit',inputs={'source':{'asset':'asset'}},parameters={'prompt':'test'}).model_dump()
        files=['edit/workflows/a.png','edit/workflows/b.png']
        workflows.change(self.f.store,self.w['id'],'package',spec=image_spec,candidate=files,quality={'passed':True,'technical_valid':True},bindings={})
        current=self.f.get()['steps'][0]
        identifier=user_decisions.create(self.f.store,self.w,current,{'kind':'selection','question':'选择图片','options':['A','B'],'output_role':'primary_image','candidates':{'A':files[0],'B':files[1]}})
        user_decisions.answer(self.f.store,self.f.turn,identifier,{'revision':0,'action':'answer','value':'B'},'owner')
        current=self.f.get()['steps'][0]
        self.assertEqual(current['status'],'verifying')
        self.assertEqual(current['bindings']['_selected_roles']['primary_image'],files[1])
        from webapp.workflow_inputs import roles
        self.assertEqual(roles(image_spec,files,current['bindings']['_selected_roles'])['primary_image'],files[1])
        self.assertFalse(any(c[0]=='submit' for c in self.f.adapter.calls))
