"""Reject unbound data before execution and repair the responsible dependency."""
import unittest
from tests import test_workflows as fixtures
from webapp import workflows,workflow_review


class WorkflowContractRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkflowTests();self.fixture.setUp()
        self.store=self.fixture.store;self.turn=self.fixture.turn
    def tearDown(self):self.fixture.tearDown()

    def test_missing_output_role_is_rejected_before_any_workflow_is_persisted(self):
        upstream=fixtures.local_step('extract')
        downstream=fixtures.local_step('assemble',['extract']);downstream['inputs']={'clip':{'step':'extract','role':'video'}}
        with self.assertRaisesRegex(ValueError,'未声明的输出角色'):
            self.fixture.submit([upstream,downstream])
        self.assertFalse(self.store.query('SELECT id FROM agent_workflows WHERE run_id=?',(self.turn,)))
        self.assertEqual(self.fixture.adapter.calls,[])
        upstream['output_roles']={'video':'final.png'}
        self.fixture.submit([upstream,downstream]);self.assertIsNotNone(self.fixture.get())

    def test_operation_output_role_uses_registered_contract(self):
        upstream={'id':'ocr','name':'ocr','kind':'operation','operation':'image.ocr','inputs':{'source':{'asset':'asset'}},
                  'goal':'Read text','acceptance':[{'kind':'json'}],'on_failure':'pause'}
        downstream=fixtures.local_step('package',['ocr']);downstream['inputs']={'text':{'step':'ocr','role':'nonexistent'}}
        with self.assertRaisesRegex(ValueError,'未声明的输出角色'):self.fixture.submit([upstream,downstream])
        downstream['inputs']['text']['role']='report'
        self.fixture.submit([upstream,downstream])

    def prepare_diagnosis(self):
        source=fixtures.local_step('source');other=fixtures.local_step('unrelated')
        finish=fixtures.local_step('finish',['source']);verify=fixtures.local_step('verify',['finish'])
        verify['on_failure']='diagnose';verify['acceptance']=[{'kind':'agent','criterion':'Title must not obscure subject'}]
        self.fixture.submit([source,other,finish,verify])
        self.fixture.adapter.validate=lambda w,s,files:{'technical_valid':True,'passed':True,'requires_agent':s['id']=='verify',
            'requires_review':False,'files':[{'name':f,'sha256':'stable'} for f in files],'checks':[]}
        for _ in range(4):self.fixture.tick()
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.turn,))
        request=workflow_review.begin(self.store,self.turn)
        return self.fixture.get(),request

    def proposal(self,w,request,target):
        step=next(s for s in w['steps'] if s['id']==target)
        return {'request_id':request['id'],'action':'repair','reason':'The title location is produced upstream; preserve inputs and acceptance.',
                'step_id':target,'replacement':{**step['spec'],'argv':['python','edit/repairs/title-safe.py']}}

    def test_diagnosis_repairs_ancestor_and_preserves_unrelated_success(self):
        w,request=self.prepare_diagnosis()
        self.assertEqual(workflow_review.context(self.store,self.turn)['repairable_steps'],['finish','source','verify'])
        old={s['id']:(s['revision'],s['result']) for s in w['steps']}
        workflow_review.stage(self.store,self.turn,self.proposal(w,request,'finish'))
        self.assertTrue(workflow_review.commit(self.store,self.turn,self.fixture.adapter))
        steps={s['id']:s for s in self.fixture.get()['steps']}
        for name in ('source','unrelated'):
            self.assertEqual(steps[name]['status'],'succeeded');self.assertEqual((steps[name]['revision'],steps[name]['result']),old[name])
        for name in ('finish','verify'):
            self.assertEqual(steps[name]['status'],'pending');self.assertEqual(steps[name]['revision'],old[name][0]+1)
        self.assertEqual(self.fixture.get()['status'],'ready')

    def test_diagnosis_cannot_repair_sibling_or_weaken_ancestor_contract(self):
        w,request=self.prepare_diagnosis()
        with self.assertRaisesRegex(ValueError,'无关分支'):
            workflow_review.stage(self.store,self.turn,self.proposal(w,request,'unrelated'))
        proposal=self.proposal(w,request,'finish');proposal['replacement']['goal']='Different unauthorized goal'
        with self.assertRaisesRegex(ValueError,'不能改变'):
            workflow_review.stage(self.store,self.turn,proposal)
        self.assertEqual(next(s for s in self.fixture.get()['steps'] if s['id']=='finish')['revision'],0)

    def test_agent_ancestor_repair_does_not_spend_descendant_repair_allowance(self):
        w,request=self.prepare_diagnosis()
        workflows.change(self.store,w['id'],'verify',repair_count=2)
        w=self.fixture.get()
        workflow_review.stage(self.store,self.turn,self.proposal(w,request,'finish'))
        self.assertTrue(workflow_review.commit(self.store,self.turn,self.fixture.adapter))
        steps={s['id']:s for s in self.fixture.get()['steps']}
        self.assertEqual(steps['finish']['repair_count'],1)
        self.assertEqual(steps['verify']['repair_count'],2)
        self.assertEqual(steps['verify']['revision'],1)
