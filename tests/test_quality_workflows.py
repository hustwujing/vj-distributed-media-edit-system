import unittest
from tests import test_workflows as fixtures
local_step=fixtures.local_step
from webapp import workflows,agent_store


class QualityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkflowTests();self.fixture.setUp()
        self.store=self.fixture.store;self.turn=self.fixture.turn
        self.report={'version':'test','technical_valid':True,'passed':True,'requires_review':False,'files':[],'checks':[]}
        self.fixture.adapter.validate=lambda w,s,files:dict(self.report)
    def tearDown(self):self.fixture.tearDown()

    def test_failed_provider_preserves_structured_code_without_resubmitting(self):
        self.fixture.submit()
        self.fixture.adapter.record={'status':'failed','error':{'code':'InvalidInput','message':'The reference does not meet the declared provider input contract.'}}
        self.fixture.tick();self.fixture.tick();self.fixture.tick()
        w=self.fixture.get();self.assertEqual(w['status'],'paused')
        diagnostic=w['steps'][0]['diagnostics'][0]
        self.assertEqual(diagnostic['code'],'InvalidInput');self.assertEqual(diagnostic['code_source'],'structured')
        self.assertIn('reference',diagnostic['provider_message'])
        self.assertEqual(self.fixture.adapter.calls.count(('submit','generate')),1)
        self.assertNotIn(('local','package'),self.fixture.adapter.calls)
        from webapp.generation_errors import task_failure
        private=task_failure({'status':'failed','error':{'code':'bad code / PRIVATE','message':'api_key=PRIVATE https://private.invalid/?signature=PRIVATE'}})
        self.assertNotIn('PRIVATE',str(private));self.assertNotIn('PRIVATE',str(private.diagnostic))

    def test_review_gate_stops_dependents_until_user_accepts(self):
        self.fixture.submit([local_step('first'),local_step('second',['first'])])
        self.report['requires_review']=True
        self.fixture.tick()
        w=self.fixture.get()
        self.assertEqual(w['steps'][0]['status'],'awaiting_review')
        self.assertEqual(w['status'],'paused')
        self.assertNotIn(('local','second'),self.fixture.adapter.calls)
        workflows.quality_action(self.store,self.turn,'first',0,'approve','owner')
        self.report['requires_review']=False
        self.fixture.tick()
        self.assertIn(('local','second'),self.fixture.adapter.calls)

    def test_failed_technical_checks_cannot_be_overridden_by_approval(self):
        self.fixture.submit([local_step()]);self.report['passed']=False;self.report['technical_valid']=False
        self.fixture.tick()
        with self.assertRaises(ValueError):workflows.quality_action(self.store,self.turn,'package',0,'approve','owner')

    def test_declared_local_repair_is_bounded_and_revalidated(self):
        step=local_step();step['repairs']=[{'reason':'修正尺寸','when':['width'],'argv':['python','edit/fixed.py']}]
        self.fixture.submit([step])
        self.fixture.adapter.validate=lambda w,s,files:{**self.report,'passed':'fixed.py' in s['spec']['argv'][-1],'checks':[{'passed':False,'code':'width'}]}
        self.fixture.tick()
        self.assertEqual(self.fixture.get()['steps'][0]['revision'],1)
        self.fixture.tick()
        current=self.fixture.get()['steps'][0]
        self.assertEqual(current['status'],'succeeded');self.assertEqual(current['repair_count'],1)
        self.assertEqual(self.fixture.adapter.calls.count(('local','package')),2)

    def test_repair_invalidates_only_dependent_revisions(self):
        self.fixture.submit([local_step('first'),local_step('second',['first'])])
        self.fixture.tick();self.report['requires_review']=True;self.fixture.tick()
        first=self.fixture.get()['steps'][0]
        replacement={**first['spec'],'argv':['python','edit/fixed.py']}
        workflows.quality_action(self.store,self.turn,'first',0,'repair','owner',replacement=replacement)
        steps=self.fixture.get()['steps']
        self.assertEqual([s['revision'] for s in steps],[1,1])
        self.assertEqual([s['repair_count'] for s in steps],[1,0])
        self.assertEqual([s['status'] for s in steps],['pending','pending'])
        self.assertEqual(len(self.store.query('SELECT * FROM workflow_step_versions')),2)

    def test_dependency_invalidations_preserve_each_steps_own_repair_allowance(self):
        self.fixture.submit([local_step('first'),local_step('second',['first']),local_step('unrelated')])
        for revision in range(2):
            first=self.fixture.get()['steps'][0]
            workflows.state(self.store,self.turn,'paused')
            workflows.quality_action(self.store,self.turn,'first',first['revision'],'repair','owner',
                replacement={**first['spec'],'argv':['python',f'edit/fix-{revision}.py']})
        first,second,unrelated=self.fixture.get()['steps']
        self.assertEqual((first['revision'],first['repair_count']),(2,2))
        self.assertEqual((second['revision'],second['repair_count']),(2,0))
        self.assertEqual((unrelated['revision'],unrelated['repair_count']),(0,0))
        workflows.state(self.store,self.turn,'paused')
        with self.assertRaisesRegex(ValueError,'次数上限'):
            workflows.quality_action(self.store,self.turn,'first',2,'repair','owner',replacement={**first['spec'],'argv':['python','edit/third.py']})
        workflows.quality_action(self.store,self.turn,'second',2,'repair','owner',replacement={**second['spec'],'argv':['python','edit/child-fix.py']})
        self.assertEqual(self.fixture.get()['steps'][1]['repair_count'],1)

    def test_expired_task_cannot_resume_quality_review(self):
        self.fixture.submit([local_step()]);self.report['requires_review']=True;self.fixture.tick()
        self.store.execute('UPDATE execution_limits SET deadline_at=0 WHERE task_id=?',(self.turn,))
        from webapp.task_lifecycle import TaskLimitReached
        with self.assertRaises(TaskLimitReached):workflows.quality_action(self.store,self.turn,'package',0,'approve','owner')


class RepairDependencyTests(unittest.TestCase):
    def test_declared_repair_invalidates_completed_dependents_only(self):
        fixture=fixtures.WorkflowTests();fixture.setUp()
        try:
            first=local_step('first');first['repairs']=[{'reason':'修复效果','when':['review'],'argv':['python','edit/fixed.py']}]
            fixture.submit([first,local_step('independent'),local_step('second',['first'])])
            for _ in range(3):fixture.tick()
            w=fixture.get();unaffected=w['steps'][1]['result']
            workflows.change(fixture.store,w['id'],'first',status='awaiting_review',candidate=w['steps'][0]['result'],result=[])
            workflows.state(fixture.store,fixture.turn,'paused')
            workflows.quality_action(fixture.store,fixture.turn,'first',0,'reject','owner','效果不符')
            steps=fixture.get()['steps']
            self.assertEqual([s['revision'] for s in steps],[1,0,1])
            self.assertEqual([s['repair_count'] for s in steps],[1,0,0])
            self.assertEqual(steps[1]['result'],unaffected)
            self.assertEqual(steps[2]['status'],'pending')
        finally:fixture.tearDown()

if __name__=='__main__':unittest.main()
