"""Contracts, evidence, read-only Agent decisions and recovery boundaries."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from tests import test_workflows as fixtures
local_step=fixtures.local_step
from webapp import workflows, workflow_review, agent_store
from webapp.quality_policy import Acceptance


class AgentReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkflowTests();self.fixture.setUp()
        self.store=self.fixture.store;self.turn=self.fixture.turn
    def tearDown(self):self.fixture.tearDown()
    def submit_review(self, failure=False, human=False):
        step=local_step('first');step['on_failure']='diagnose'
        step['acceptance']=[{'kind':'image','width':10},{'kind':'agent','criterion':'Preserve subject'}]
        if human:step['acceptance'].append({'kind':'review','criterion':'Approve style'})
        self.fixture.submit([step,local_step('second',['first'])])
        self.fixture.adapter.validate=lambda w,s,files:{'technical_valid':True,'passed':not failure,
            'requires_agent':s['id']=='first','requires_review':human and s['id']=='first',
            'files':[{'name':f,'sha256':'stable'} for f in files],
            'checks':[{'passed':False,'code':'width'}] if failure else []}
        self.fixture.tick()
        return self.fixture.get()
    def begin(self):
        self.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.turn,))
        return workflow_review.begin(self.store,self.turn)
    def decision(self, action='pass', **kwargs):
        w=self.fixture.get();s=w['steps'][0]
        return {'request_id':w['agent_review']['id'],'action':action,'reason':'Inspected actual output',
                'evidence':[{'step_id':s['id'],'criterion':1,'files':s['candidate'] or s['result'],'observation':'Subject is intact'}],**kwargs}

    def parallel_human_gate(self):
        steps=[local_step('left'),local_step('right'),local_step('independent')]
        steps[1]['on_failure']='diagnose'
        self.fixture.submit(steps);initial=self.fixture.get()
        self.fixture.adapter.validate=lambda w,step,files:{'technical_valid':True,'passed':True,
            'requires_agent':step['id']=='left','requires_review':step['id']=='right','files':[],'checks':[]}
        # Both branches start before either publishes its gate; the human
        # branch finishes last, while the first Agent review is still queued.
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(2);left_done=threading.Event()
        def local(w,step,cancelled):
            barrier.wait(timeout=5)
            if step['id']=='right':self.assertTrue(left_done.wait(5))
            return ['edit/workflows/'+step['id']+'/final.png']
        self.fixture.adapter.local=local
        def run(step):
            try:self.fixture.runner.step(initial,step)
            finally:
                if step['id']=='left':left_done.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run,step) for step in initial['steps'][:2]]
            for future in futures:future.result(timeout=10)
        self.assertEqual(self.fixture.get()['status'],'paused')
        return self.fixture.get()['agent_review']['id']

    def test_user_approval_reactivates_existing_sibling_review(self):
        from webapp.task_leases import Coordinator
        request=self.parallel_human_gate()
        workflows.quality_action(self.store,self.turn,'right',0,'approve','owner')
        coordinator=Coordinator(self.store,'human-gate','test')
        rows=coordinator.candidates()
        self.assertEqual(self.fixture.get()['status'],'reviewing')
        self.assertEqual(self.fixture.get()['agent_review']['id'],request)
        self.assertTrue(any(r['id']==self.turn and r['workflow_status']=='reviewing' for r in rows))
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_agent_reviews')),1)

    def test_user_rejection_preserves_and_wakes_both_pending_diagnoses(self):
        from webapp.task_leases import Coordinator
        request=self.parallel_human_gate()
        workflows.quality_action(self.store,self.turn,'right',0,'reject','owner','Please inspect the result')
        Coordinator(self.store,'human-gate','test').candidates()
        self.assertEqual(self.fixture.get()['status'],'reviewing')
        self.assertEqual(self.fixture.get()['agent_review']['id'],request)
        self.assertEqual(self.fixture.get()['steps'][1]['status'],'diagnosing')

    def test_stale_step_claim_cannot_bypass_pending_sibling_review(self):
        from webapp.task_leases import Coordinator
        self.parallel_human_gate()
        workflows.quality_action(self.store,self.turn,'right',0,'approve','owner')
        coordinator=Coordinator(self.store,'stale-candidate','test')
        lease=coordinator.claim(self.turn,'workflow',step_id='independent')
        try:self.assertIsNone(lease)
        finally:
            if lease:lease.release()

    def test_parallel_diagnoses_are_durable_and_drained_without_rerunning_siblings(self):
        from webapp.task_leases import Coordinator
        steps=[local_step('left'),local_step('right'),local_step('join',['left','right'])]
        for step in steps[:2]:step['on_failure']='diagnose'
        self.fixture.submit(steps);w=self.fixture.get()
        for step in w['steps'][:2]:
            workflows.change(self.store,w['id'],step['id'],status='paused',error_code='local')
            workflow_review.queue(self.store,self.fixture.get(),step,'diagnose')
        self.assertEqual([s['status'] for s in self.fixture.get()['steps'][:2]],['diagnosing','diagnosing'])
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_agent_reviews')),1)
        coordinator=Coordinator(self.store,'review-drain','test')
        for name in ('left','right'):
            self.begin();w=self.fixture.get();request=w['agent_review']
            self.assertEqual(request['step'],name)
            step=next(s for s in w['steps'] if s['id']==name)
            workflow_review.stage(self.store,self.turn,{'request_id':request['id'],'action':'repair',
                'reason':'Correct local command','step_id':name,'replacement':{**step['spec'],'argv':['python','edit/repaired.py']}})
            workflow_review.commit(self.store,self.turn,self.fixture.adapter)
            coordinator.candidates()
        self.assertIsNone(self.fixture.get()['agent_review'])
        for _ in range(3):self.fixture.tick()
        self.assertEqual([s['status'] for s in self.fixture.get()['steps']],['succeeded']*3)
        self.assertEqual([s['revision'] for s in self.fixture.get()['steps']],[1,1,2])
        self.assertEqual([self.fixture.adapter.calls.count(('local',name)) for name in ('left','right','join')],[1,1,1])

    def test_parallel_content_reviews_preserve_each_candidate(self):
        from webapp.task_leases import Coordinator
        self.fixture.submit([local_step('left'),local_step('right')]);w=self.fixture.get()
        for step in w['steps']:
            files=['edit/'+step['id']+'.png']
            workflows.change(self.store,w['id'],step['id'],candidate=files,quality={
                'technical_valid':True,'passed':True,'requires_agent':True,'files':[],'checks':[]})
            workflow_review.queue(self.store,self.fixture.get(),step,'review')
        self.assertEqual([s['status'] for s in self.fixture.get()['steps']],['agent_review']*2)
        coordinator=Coordinator(self.store,'review-drain','test')
        for name in ('left','right'):
            self.begin();w=self.fixture.get();request=w['agent_review'];self.assertEqual(request['step'],name)
            workflow_review.stage(self.store,self.turn,{'request_id':request['id'],'action':'pass','reason':'Checked actual candidate',
                'evidence':[{'step_id':name,'files':['edit/'+name+'.png'],'observation':'Read candidate pixels'}]})
            workflow_review.commit(self.store,self.turn,self.fixture.adapter)
            coordinator.candidates()
        self.assertEqual([s['result'] for s in self.fixture.get()['steps']],[['edit/left.png'],['edit/right.png']])
        self.assertFalse(any(c[0]=='local' for c in self.fixture.adapter.calls))

    def test_sibling_user_request_survives_repair_and_remains_a_gate(self):
        from webapp.user_decisions import create,pending
        self.submit_review(failure=True);w=self.fixture.get();second=w['steps'][1]
        # An independent branch asks a question while the first reviewer runs.
        second['spec']['depends_on']=[]
        workflows.change(self.store,w['id'],'second',spec=second['spec'],status='awaiting_review',error='Choose a result')
        identifier=create(self.store,w,second,{'kind':'clarification','question':'Choose a result'})
        self.begin();first=self.fixture.get()['steps'][0]
        workflow_review.stage(self.store,self.turn,self.decision('repair',replacement={**first['spec'],'argv':['python','edit/repaired.py']}))
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertEqual(pending(self.store,w['id'])[0]['id'],identifier)
        self.assertTrue(workflow_review.reconcile(self.store,self.turn))
        self.assertEqual(self.fixture.get()['status'],'paused')
        self.assertEqual(self.fixture.get()['steps'][1]['status'],'awaiting_review')
        self.assertFalse(workflow_review.reconcile(self.store,self.turn))

    def test_content_review_blocks_downstream_then_commits(self):
        w=self.submit_review()
        self.assertEqual(w['steps'][0]['status'],'agent_review')
        self.fixture.tick();self.assertNotIn(('local','second'),self.fixture.adapter.calls)
        self.begin();workflow_review.stage(self.store,self.turn,self.decision())
        self.assertEqual(self.fixture.get()['steps'][0]['status'],'agent_review')
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertEqual(self.fixture.get()['steps'][0]['status'],'succeeded')
        self.fixture.tick();self.assertIn(('local','second'),self.fixture.adapter.calls)

    def test_agent_cannot_bypass_human_gate(self):
        self.submit_review(human=True);self.begin()
        workflow_review.stage(self.store,self.turn,self.decision())
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertEqual(self.fixture.get()['steps'][0]['status'],'awaiting_review')
        self.assertEqual(self.fixture.get()['status'],'paused')

    def test_missing_evidence_and_wrong_criterion_rejected(self):
        self.submit_review();self.begin()
        for evidence in ([],[{'step_id':'first','criterion':0,'files':['edit/foreign.png'],'observation':'good'}]):
            with self.assertRaises(ValueError):workflow_review.stage(self.store,self.turn,self.decision(evidence=evidence))

    def test_technical_failure_cannot_be_approved(self):
        self.submit_review(failure=True);self.begin()
        with self.assertRaises(ValueError):workflow_review.stage(self.store,self.turn,self.decision())

    def test_changed_bytes_prevent_approval(self):
        self.submit_review();self.begin();workflow_review.stage(self.store,self.turn,self.decision())
        original=self.fixture.adapter.validate
        self.fixture.adapter.validate=lambda w,s,files:{**original(w,s,files),'files':[{'name':f,'sha256':'changed'} for f in files]}
        with self.assertRaises(ValueError):workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertNotEqual(self.fixture.get()['steps'][0]['status'],'succeeded')

    def test_cancel_wins_over_staged_decision(self):
        self.submit_review();self.begin();workflow_review.stage(self.store,self.turn,self.decision())
        workflows.cancel(self.store,self.turn)
        self.assertFalse(workflow_review.commit(self.store,self.turn,self.fixture.adapter))
        self.assertEqual(self.fixture.get()['status'],'cancelled')
        self.assertTrue(self.store.query('SELECT request_id FROM execution_cancel_requests WHERE task_id=?',(self.turn,)))
        self.assertFalse(any(a.get('workflow_delivery') for a in agent_store.turn(self.store,self.turn)['artifacts']))

    def test_expired_budget_cannot_apply_decision(self):
        self.submit_review();self.begin();workflow_review.stage(self.store,self.turn,self.decision())
        self.store.execute('UPDATE execution_limits SET deadline_at=0 WHERE task_id=?',(self.turn,))
        from webapp.task_lifecycle import TaskLimitReached
        with self.assertRaises(TaskLimitReached):workflow_review.commit(self.store,self.turn,self.fixture.adapter)

    def test_recovery_discards_uncommitted_decision_without_reexecution(self):
        self.submit_review();self.begin();workflow_review.stage(self.store,self.turn,self.decision())
        self.fixture.runner.recover(self.turn)
        request=self.fixture.get()['agent_review']
        self.assertEqual(request['status'],'pending');self.assertEqual(request['decision'],{})
        self.assertEqual(self.fixture.adapter.calls.count(('local','first')),1)

    def test_review_recovery_is_bounded(self):
        self.submit_review()
        for _ in range(3):self.begin();self.fixture.runner.recover(self.turn)
        self.assertEqual(self.fixture.get()['status'],'failed')
        self.assertFalse(workflows.public(self.fixture.get())['resumable'])

    def test_no_decision_is_not_success_and_can_resume(self):
        self.submit_review();self.begin()
        self.assertFalse(workflow_review.commit(self.store,self.turn,self.fixture.adapter))
        self.assertEqual(self.fixture.get()['status'],'paused')
        workflows.resume(self.store,self.turn)
        self.assertEqual(self.fixture.get()['status'],'reviewing')

    def test_exhausted_model_budget_is_not_advertised_as_resumable(self):
        from webapp.task_leases import TaskQuotaReached
        self.submit_review();self.begin()
        self.assertFalse(workflow_review.commit(self.store,self.turn,self.fixture.adapter))
        self.store.execute('INSERT INTO execution_budgets VALUES (?,?,?)',(self.turn,'model',3))
        with patch.dict('os.environ',{'VIDEO_USE_TASK_MODEL_CALLS':'3'}):
            before=self.fixture.get();view=workflows.public(before)
            self.assertFalse(view['resumable']);self.assertEqual(view['resume_block']['scope'],'task')
            with self.assertRaises(TaskQuotaReached):workflows.resume(self.store,self.turn)
            self.assertEqual(self.fixture.get()['steps'][0]['candidate'],before['steps'][0]['candidate'])
            self.assertEqual(self.store.query('SELECT used FROM execution_budgets WHERE turn_id=?',(self.turn,))[0]['used'],3)

    def test_diagnosis_repairs_preserving_contract_and_dependents(self):
        self.submit_review(failure=True);self.begin()
        s=self.fixture.get()['steps'][0]
        replacement={**s['spec'],'argv':['python','edit/repairs/new.py']}
        workflow_review.stage(self.store,self.turn,self.decision('repair',replacement=replacement))
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        steps=self.fixture.get()['steps']
        self.assertEqual([s['revision'] for s in steps],[1,1])
        self.assertEqual(steps[0]['spec']['acceptance'],replacement['acceptance'])
        self.assertEqual(self.fixture.get()['status'],'ready')

    def test_repair_cannot_weaken_acceptance(self):
        self.submit_review(failure=True);self.begin()
        s=self.fixture.get()['steps'][0]
        with self.assertRaises(ValueError):workflow_review.stage(self.store,self.turn,self.decision('repair',replacement={**s['spec'],'acceptance':[]}))
        workflows.state(self.store,self.turn,'paused')
        with self.assertRaises(ValueError):workflows.quality_action(self.store,self.turn,s['id'],0,'repair','owner',replacement={**s['spec'],'acceptance':[]})

    def test_stale_revision_rejected(self):
        self.submit_review();self.begin();decision=self.decision()
        workflows.change(self.store,self.fixture.get()['id'],'first',revision=1)
        with self.assertRaises(ValueError):workflow_review.stage(self.store,self.turn,decision)

    def test_execution_failure_retains_diagnostics_and_dispatches_agent(self):
        step=local_step();step['on_failure']='diagnose';step['retry_safe']=False
        self.fixture.submit([step]);self.fixture.adapter.local_error=True;self.fixture.tick()
        w=self.fixture.get()
        self.assertEqual(w['agent_review']['purpose'],'diagnose')
        self.assertEqual(w['steps'][0]['diagnostics'][0]['phase'],'local')
        self.assertIn('script failed',w['steps'][0]['diagnostics'][0]['message'])

    def test_final_requires_explicit_evidence_and_deliveries(self):
        self.fixture.submit([local_step()]);self.fixture.tick();self.fixture.tick();self.begin()
        w=self.fixture.get();files=w['steps'][0]['result']
        raw={'request_id':w['agent_review']['id'],'action':'pass','reason':'Result checked','evidence':[{'step_id':'package','files':files,'observation':'Matches goal'}]}
        with self.assertRaises(ValueError):workflow_review.stage(self.store,self.turn,raw)
        workflow_review.stage(self.store,self.turn,{**raw,'deliveries':files})
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertEqual(agent_store.turn(self.store,self.turn)['status'],'succeeded')

    def test_repair_selection_matches_error(self):
        step=local_step();step['repairs']=[{'reason':'audio','when':['audio'],'argv':['python','audio.py']},
            {'reason':'size','when':['width'],'argv':['python','size.py']}]
        self.fixture.submit([step]);w=self.fixture.get()
        self.assertFalse(workflows.queue_auto_repair(self.store,w,w['steps'][0],{'checks':[{'passed':False,'code':'height'}]}))
        self.assertTrue(workflows.queue_auto_repair(self.store,w,w['steps'][0],{'checks':[{'passed':False,'code':'width'}]}))
        self.assertEqual(self.fixture.get()['steps'][0]['spec']['argv'],['python','size.py'])

    def test_native_cutout_recovers_without_remote_submission(self):
        step={'id':'cutout','name':'Cutout','kind':'operation','operation':'image.remove_background','goal':'Extract foreground',
              'inputs':{'source':{'asset':'asset'}},'retry_safe':True,'on_failure':'pause',
              'acceptance':[{'kind':'image','file':'foreground.png','width':8},{'kind':'image','file':'mask.png','width':8}]}
        self.fixture.submit([step,local_step('compose',['cutout'])])
        calls=[]
        self.fixture.adapter.fixed_image=lambda w,s,cancel: calls.append(s['id']) or ['edit/workflows/cutout/foreground.png','edit/workflows/cutout/mask.png']
        workflows.change(self.store,self.fixture.get()['id'],'cutout',status='running',attempts=1)
        self.fixture.runner.recover(self.turn);self.fixture.tick()
        self.assertEqual(calls,['cutout'])
        self.assertFalse(any(c[0]=='submit' for c in self.fixture.adapter.calls))
        self.fixture.runner.recover(self.turn);self.fixture.tick()
        self.assertEqual(calls,['cutout'])
        self.assertIn(('local','compose'),self.fixture.adapter.calls)

    def test_final_question_has_a_candidate_and_user_feedback_gate(self):
        self.fixture.submit([local_step()]);self.fixture.tick();self.fixture.tick();self.begin()
        w=self.fixture.get();files=w['steps'][0]['result']
        workflow_review.stage(self.store,self.turn,{'request_id':w['agent_review']['id'],'action':'needs_user',
            'reason':'Please choose whether to keep the current style'})
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        s=self.fixture.get()['steps'][0]
        self.assertEqual(s['status'],'awaiting_review');self.assertEqual(s['candidate'],files)
        self.assertEqual(s['result'],[])
        workflows.quality_action(self.store,self.turn,'package',0,'approve','owner')
        self.fixture.tick()
        self.assertEqual(self.fixture.get()['agent_review']['purpose'],'final')

    def test_final_stop_cannot_be_resumed_as_success(self):
        self.fixture.submit([local_step()]);self.fixture.tick();self.fixture.tick();self.begin()
        w=self.fixture.get()
        workflow_review.stage(self.store,self.turn,{'request_id':w['agent_review']['id'],'action':'stop','reason':'Cannot satisfy goal within budget'})
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertFalse(workflows.public(self.fixture.get())['resumable'])
        self.assertEqual(self.fixture.get()['status'],'failed')

    def test_duplicate_commit_does_not_demote_completed_workflow(self):
        self.fixture.submit([local_step()]);self.fixture.tick();self.fixture.tick();self.begin()
        w=self.fixture.get();files=w['steps'][0]['result']
        workflow_review.stage(self.store,self.turn,{'request_id':w['agent_review']['id'],'action':'pass',
            'reason':'checked','deliveries':files,'evidence':[{'step_id':'package','files':files,'observation':'good'}]})
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        workflow_review.commit(self.store,self.turn,self.fixture.adapter)
        self.assertEqual(self.fixture.get()['status'],'succeeded')


class ContractTests(unittest.TestCase):
    def test_empty_acceptance_and_noop_conditions_rejected(self):
        step=local_step();step['acceptance']=[]
        with self.assertRaisesRegex(ValueError,'acceptance'):workflows.validate_plan({'key':'a','name':'a','steps':[step]})
        for rule in ({'kind':'file','criterion':'must match user intent'}, {'kind':'text','contains':['']}, {'kind':'image','audio':True}):
            with self.assertRaises(ValueError):Acceptance.model_validate(rule)

    def test_real_checker_checks_pixels_and_reports_agent_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            Image.new('RGBA',(12,8),(0,0,0,0)).save(Path(directory,'image.png'))
            rules=[Acceptance(kind='image',width=10).model_dump(),Acceptance(kind='agent',criterion='Subject intact').model_dump()]
            result=subprocess.run([sys.executable,str(Path('webapp/quality_check.py').resolve())],cwd=directory,
                input=json.dumps({'files':['image.png'],'acceptance':rules}),text=True,capture_output=True,check=True)
            report=json.loads(result.stdout)
            self.assertFalse(report['passed']);self.assertTrue(report['requires_agent'])
            self.assertEqual(report['checks'][0]['codes'],['width'])
            self.assertEqual(report['files'][0]['width'],12)

    def test_local_worker_rejects_corrupt_nonempty_mkv(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,str(Path('webapp/workflow_io.py').resolve())],cwd=directory,
                input=json.dumps({'mode':'local','output':'edit/workflows/test','argv':[sys.executable,'-c',
                    "from pathlib import Path; Path(r'{output}/bad.mkv').write_text('not media')"],'outputs':['bad.mkv']}),
                text=True,capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('ffprobe',json.loads(result.stdout)['message'])

    def test_real_worker_reports_exit_code_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,str(Path('webapp/workflow_io.py').resolve())],cwd=directory,
                input=json.dumps({'mode':'local','output':'edit/workflows/test','argv':[sys.executable,'-c','print("bad argument");raise SystemExit(7)'],'outputs':['x.png']}),
                text=True,capture_output=True)
            self.assertEqual(result.returncode,1)
            record=json.loads(result.stdout)
            self.assertEqual(record['exit_code'],7);self.assertIn('bad argument',record['log_tail'])

if __name__=='__main__':unittest.main()
