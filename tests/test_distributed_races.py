import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from tests.distributed_support import PostgresServer
from webapp import agent_store,workflows,workflow_review
from webapp.store import Store
from webapp.task_leases import Coordinator,LeaseLost
from webapp.cancellation import Monitor


class DistributedRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pg=PostgresServer()
    @classmethod
    def tearDownClass(cls):cls.pg.close()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.a=Store(root/'a',dsn=self.pg.dsn);agent_store.initialize(self.a)
        self.b=Store(root/'b',dsn=self.pg.dsn)
        self.project=self.a.add_project(self.temp.name,'races')['id']
        plan=agent_store.add_turn(self.a,self.project,'plan','plan');agent_store.finish(self.a,plan,'succeeded','approved')
        self.task=agent_store.add_turn(self.a,self.project,'execute','execute',plan_id=plan)
        self.a.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
        self.ca=Coordinator(self.a,self.temp.name+'a','test');self.cb=Coordinator(self.b,self.temp.name+'b','test')
        self.source=self.project+'src'
        self.a.add_asset(self.project,self.source,'source.png','source.png',{'kind':'image'})
    def tearDown(self):self.temp.cleanup()
    def submit(self,remote=False,implementation=None):
        steps=[]
        for name in ('left','right'):
            step={'id':name,'name':name,'goal':'test branch','on_failure':'pause','acceptance':[{'kind':'image','width':8}]}
            if remote:step.update(kind='operation',operation='image.edit',inputs={'source':{'asset':self.source}},parameters={'prompt':'blue'})
            else:step.update(kind='script',argv=['python','edit/test.py'],outputs=['output.png'],retry_safe=True)
            steps.append(step)
        if implementation:
            import hashlib
            from webapp.execution_intent import Intent
            intent=Intent(route='workflow',operations=['image.edit','image.edit'],implementations={'image.edit':implementation},goal='test selected provider',estimated_seconds=300).model_dump_json()
            plan=self.a.query('SELECT plan_id FROM agent_turns WHERE id=?',(self.task,))[0]['plan_id']
            self.a.execute('INSERT INTO execution_intents VALUES (?,?,?,?)',(plan,intent,hashlib.sha256(intent.encode()).hexdigest(),self.a.now()))
        workflows.submit(self.a,self.task,{'key':'races','name':'races','steps':steps})
        workflows.state(self.a,self.task,'ready')
        return workflows.get(self.a,self.task)

    def test_provider_reservation_counts_parallel_siblings_but_allows_polling(self):
        w=self.submit(remote=True)
        with patch.dict(os.environ,{'VIDEO_USE_PROVIDER_CONCURRENCY':'1'}):
            first=self.ca.claim(self.task,'workflow',step_id='left')
            self.assertIsNotNone(first)
            self.assertIsNone(self.cb.claim(self.task,'workflow',step_id='right'))
            self.a.execute('''INSERT INTO workflow_provider_calls(request_key,workflow,step,revision,attempt,provider,operation,state,external_id,inputs,created,updated)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(w['id']+':left:0',w['id'],'left',0,1,'https://provider.invalid','image.edit','waiting','job','{}',0,0))
            self.a.execute('UPDATE workflow_provider_calls SET implementation=? WHERE workflow=?',(json.dumps({'concurrency_group':'edit'}),w['id']))
            workflows.change(self.a,w['id'],'left',status='waiting',external_id='job',next_poll=0)
            first.release()
            self.assertIsNone(self.cb.claim(self.task,'workflow',step_id='right'))
            polling=self.cb.claim(self.task,'workflow',step_id='left')
            self.assertIsNotNone(polling);polling.release()
            self.a.execute("UPDATE workflow_provider_calls SET state='succeeded' WHERE workflow=?",(w['id'],))
            second=self.cb.claim(self.task,'workflow',step_id='right')
            self.assertIsNotNone(second);second.release()

    def test_provider_limit_uses_bound_alternative_not_default_backend(self):
        from webapp import operations,provider_calls
        from webapp.operation_lifecycle import Implementation,Lifecycle,register,IMPLEMENTATIONS
        identifier='alternative_image_provider@2'
        register(Implementation(identifier,Lifecycle.ASYNC))
        operations.register_implementation('image.edit',identifier,concurrency_group='alternative-provider',provider_identity='alternative-provider')
        first=None
        try:
            w=self.submit(remote=True,implementation=identifier)
            self.assertEqual(w['steps'][0]['spec']['implementation'],identifier)
            with patch.dict(os.environ,{'VIDEO_USE_PROVIDER_CONCURRENCY':'1'}):
                first=self.ca.claim(self.task,'workflow',step_id='left')
                self.assertIsNotNone(first)
                with first.bind():
                    provider_calls.intent(self.a,w,w['steps'][0],'http://127.0.0.1:9876','image.edit')
                    provider_calls.update(self.a,w,w['steps'][0],'waiting','existing-job')
                    workflows.change(self.a,w['id'],'left',status='waiting',external_id='existing-job')
                first.release();first=None
                record=self.a.query('SELECT implementation FROM workflow_provider_calls WHERE workflow=?',(w['id'],))[0]
                self.assertEqual(json.loads(record['implementation'])['concurrency_group'],'alternative-provider')
                self.assertIsNone(self.cb.claim(self.task,'workflow',step_id='right'))
        finally:
            if first:first.release()
            IMPLEMENTATIONS.pop(identifier,None)
            operations._IMPLEMENTATION_BINDINGS.get('image.edit',{}).pop(identifier,None)
            operations._VARIANT_CONTRACTS.pop(('image.edit',identifier),None)

    def test_cancel_fences_both_parallel_attempts_without_runtime_cleanup_rows(self):
        self.submit()
        first=self.ca.claim(self.task,'workflow',step_id='left')
        second=self.cb.claim(self.task,'workflow',step_id='right')
        monitor=Monitor(self.a,interval=.05)
        try:
            with monitor.register_lease(first,'http') as left,monitor.register_lease(second,'http') as right:
                a,b=threading.Event(),threading.Event();left.install(a.set);right.install(b.set)
                workflows.cancel(self.b,self.task)
                self.assertTrue(a.wait(2));self.assertTrue(b.wait(2))
                for lease,store in ((first,self.a),(second,self.b)):
                    with lease.bind(),self.assertRaises(LeaseLost):
                        store.execute("UPDATE agent_steps SET status='succeeded' WHERE id='left' AND workflow IN (SELECT id FROM agent_workflows WHERE turn_id=?)",(self.task,))
                self.assertIsNone(self.cb.claim(self.task,'workflow',step_id='right'))
        finally:
            monitor.close();first.release();second.release()

    def test_remote_offline_cleanup_does_not_permanently_block_project(self):
        old=self.ca.claim(self.task,'agent')
        self.a.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
                       (old.attempt,self.task,self.ca.node,self.a.now()))
        self.a.execute("UPDATE agent_turns SET status='cancelled' WHERE id=?",(self.task,));old.release()
        self.a.execute('UPDATE execution_workers SET heartbeat=0 WHERE id=?',(self.ca.worker,))
        new=agent_store.add_turn(self.b,self.project,'plan','new')
        self.assertIsNone(self.ca.claim(new,'agent'))
        replacement=self.cb.claim(new,'agent')
        self.assertIsNotNone(replacement)
        self.assertEqual(self.a.query('SELECT state FROM execution_cleanup WHERE attempt=?',(old.attempt,))[0]['state'],'allocated')
        replacement.release()

    def test_review_snapshot_freezes_after_siblings_finish(self):
        w=self.submit()
        workflows.change(self.a,w['id'],'left',status='verifying',candidate=['edit/left.png'])
        workflow_review.queue(self.a,workflows.get(self.a,self.task),w['steps'][0],'review')
        before=workflow_review.current(self.a,w['id'])['snapshot']
        workflows.change(self.b,w['id'],'right',status='succeeded',result=['edit/right.png'])
        request=workflow_review.begin(self.a,self.task)
        self.assertNotEqual(request['snapshot'],before)
        self.assertEqual(request['snapshot'],workflow_review.snapshot(workflows.get(self.a,self.task)))

    def test_node_capabilities_and_drain_are_enforced_at_claim(self):
        self.submit(remote=True)
        with patch.dict(os.environ,{'VIDEO_USE_NODE_OPERATIONS':'media.render'}):
            self.assertIsNone(self.ca.claim(self.task,'workflow',step_id='left'))
        self.ca.heartbeat(draining=True)
        self.assertIsNone(self.ca.claim(self.task,'workflow',step_id='left'))
        lease=self.cb.claim(self.task,'workflow',step_id='left')
        self.assertIsNotNone(lease);lease.release()

    def test_runtime_environment_change_cannot_claim_pinned_local_step(self):
        w=self.submit()
        workflows.change(self.a,w['id'],'left',bindings={'_environment':'old-incompatible-environment'})
        self.assertIsNone(self.cb.claim(self.task,'workflow',step_id='left'))
        lease=self.cb.claim(self.task,'workflow',step_id='right')
        self.assertIsNotNone(lease);lease.release()

    def test_progress_events_identify_step_attempt_revision_and_implementation(self):
        self.submit(remote=True);lease=self.ca.claim(self.task,'workflow',step_id='left')
        try:
            with lease.bind():agent_store.event(self.a,self.task,'progress',{'text':'submitting','phase':'submit'})
            row=self.b.query('SELECT payload FROM agent_events WHERE turn_id=? ORDER BY seq DESC LIMIT 1',(self.task,))[0]
            event=json.loads(row['payload']);context=event['execution']
            self.assertEqual(context['attempt_id'],lease.attempt);self.assertEqual(context['step_id'],'left')
            self.assertEqual(context['operation'],'image.edit');self.assertEqual(context['revision'],0)
            self.assertEqual(context['implementation'],'image_async@1');self.assertEqual(event['phase'],'submit')
            from webapp.execution_metrics import phase
            with lease.bind(),phase('poll'):pass
            metric=self.b.query('SELECT calls,seconds,step FROM execution_phase_metrics WHERE attempt=? AND phase=?',(lease.attempt,'poll'))[0]
            self.assertEqual(metric['calls'],1);self.assertEqual(metric['step'],'left');self.assertGreaterEqual(metric['seconds'],0)
        finally:lease.release()


if __name__=='__main__':unittest.main()
