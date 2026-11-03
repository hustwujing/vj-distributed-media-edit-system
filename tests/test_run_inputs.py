"""Inbox ordering and barriers; no model or provider substitutions in production."""
import json
import unittest
import uuid
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning,run_inputs
from webapp.task_leases import Coordinator


class RunInputTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.f.tick()
        self.f.propose({'proposal_id':'initial','base_graph_version':0,'reason':'Two independent reports',
            'add_tasks':[fixtures.script('left'),fixtures.script('right')],'sealed':True})
        self.run=self.f.tick();self.coordinator=Coordinator(self.store,'input-test','test')

    def send(self,text='Use a simpler layout',key=None):
        return run_inputs.accept(self.store,self.run['id'],'owner',{'request_id':key or uuid.uuid4().hex,'content':text})

    def reply(self,kind='CONTINUE',**extra):
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],kind=kind,**extra))
        finally:lease.release()

    def test_input_first_blocks_previously_ready_task_and_is_idempotent(self):
        key=uuid.uuid4().hex;first=self.send(key=key)
        self.assertEqual(first['id'],self.send(key=key)['id'])
        with self.assertRaises(ValueError):self.send('Different request',key)
        self.assertIsNone(self.coordinator.claim(self.run['id'],'workflow',step_id='left'))
        run=self.f.tick()
        self.assertEqual(run['input_sequence'],1)
        self.assertEqual([job['control_kind'] for job in run['control_jobs'] if job['status']=='ready'],['PLAN'])
        self.assertEqual(run['drain_set'],[])

    def test_claim_first_drains_original_revision_and_holds_sibling(self):
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id='left');self.assertIsNotNone(lease)
        try:
            self.send();lease.check()
            run=graph.get(self.store,self.run['id'])
            self.assertEqual([(x['task_id'],x['revision']) for x in run['drain_set']],[('left',0)])
            self.assertIsNone(self.coordinator.claim(run['id'],'workflow',step_id='right'))
            self.f.tick()
            self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))
            # Protocol fixture represents a normal completed drain result.
            self.store.execute("UPDATE run_tasks SET status='succeeded' WHERE run_id=? AND id='left'",(run['id'],))
        finally:lease.release()
        self.f.tick();self.reply();run=self.f.tick()
        self.assertIsNone(run['input_barrier_sequence'])
        self.assertEqual(run['graph_version'],1)
        self.assertTrue(run['sealed'])
        self.assertEqual(run['tasks'][0]['revision'],0)
        next_lease=self.coordinator.claim(run['id'],'workflow',step_id='right')
        self.assertIsNotNone(next_lease);next_lease.release()

    def test_input_during_planning_stays_in_the_next_batch(self):
        first=self.send();self.f.tick();second=self.send('Also keep the original dimensions')
        self.reply();run=self.f.tick()
        rows=self.store.query('SELECT sequence,status FROM run_inputs ORDER BY sequence')
        self.assertEqual(rows[0],{'sequence':1,'status':'APPLIED'})
        self.assertEqual(rows[1],{'sequence':2,'status':'PLANNING'})
        self.assertEqual(run['input_barrier_sequence'],2)
        request=self.store.query("SELECT snapshot_json FROM planner_runs WHERE status='pending'")[0]
        batch=json.loads(request['snapshot_json'])['input_batch']
        self.assertEqual([item['id'] for item in batch['inputs']],[second['id']])
        self.assertIsNone(self.coordinator.claim(run['id'],'workflow',step_id='right'))

    def test_pending_user_decision_prevents_safe_point(self):
        self.f.orchestrator.decision(self.run,'quality',{'task_id':'left','revision':0})
        self.send();run=self.f.tick()
        self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(run['terminal_reason'],'input_waiting_user_decision')
        self.assertFalse(self.store.query("SELECT id FROM planner_runs WHERE status='pending'"))

    def test_pending_input_prevents_old_graph_publication(self):
        self.send()
        self.f.orchestrator.publish(self.run,{'decision_json':'{"deliverables":[]}'} )
        run=graph.get(self.store,self.run['id'])
        self.assertIsNone(run['delivered_at'])
        self.assertNotEqual(run['status'],'SUCCEEDED')

    def test_manager_question_does_not_mutate_the_graph_or_consume_input(self):
        self.send();self.f.tick()
        self.reply('REQUEST_USER_INPUT',question='Which background should be used?',options=['white','black'])
        run=self.f.tick()
        self.assertEqual(run['graph_version'],1)
        self.assertEqual(run['status'],'WAITING_USER')
        self.assertEqual(self.store.query('SELECT status FROM run_inputs')[0]['status'],'ACCEPTED')
        self.assertEqual(self.store.query("SELECT kind FROM workflow_decisions WHERE status='pending'")[0]['kind'],'input_question')

    def test_targeted_input_freezes_descendant_scope_and_cannot_revise_sibling(self):
        self.store.execute('INSERT INTO workflow_dependencies(run_id,upstream_task_id,downstream_task_id,created_graph_version) VALUES (?,?,?,1)',
                           (self.run['id'],'left','right'))
        result=run_inputs.accept(self.store,self.run['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Change only the final report','targets':['right']})
        self.assertEqual(result['context']['allowed_revision_tasks'],['right'])
        self.assertEqual(result['context']['base_task_revisions'],{'left':0,'right':0})
        self.f.tick()
        revision={**next(task['spec'] for task in self.run['tasks'] if task['id']=='left'),'argv':['python','-c','print("changed")']}
        self.f.propose({'proposal_id':'wrong-branch','base_graph_version':1,'reason':'Change unrelated upstream',
                        'revise_tasks':[revision],'sealed':True})
        run=self.f.tick()
        self.assertEqual(run['graph_version'],1)
        self.assertEqual([task['revision'] for task in run['tasks']],[0,0])
        self.assertIsNotNone(run['input_barrier_sequence'])

    def test_successor_input_deduplicates_and_requires_a_fresh_scope_confirmation(self):
        original=self.run
        self.store.execute("UPDATE runs SET status='SUCCEEDED',delivered_at=? WHERE id=?",(self.store.now(),original['id']))
        first=run_inputs.accept(self.store,original['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Make a second report','targets':['left']})
        second=run_inputs.accept(self.store,original['id'],'owner',{
            'request_id':uuid.uuid4().hex,'content':'Keep it concise'})
        self.assertEqual(first['run_id'],second['run_id'])
        self.assertNotEqual(first['run_id'],original['id'])
        self.assertEqual(first['context']['allowed_revision_tasks'],[])
        self.assertEqual(first['context']['predecessor_targets'][0]['task_id'],'left')
        self.run=graph.get(self.store,first['run_id']);self.f.run=self.run
        self.assertFalse(self.run['scope_confirmed'])
        self.assertEqual(self.run['tasks'],[])
        self.f.tick()
        request=self.store.query("SELECT * FROM planner_runs WHERE run_id=? AND status='pending'",(self.run['id'],))[0]
        lease=self.coordinator.claim(self.run['id'],'workflow',step_id=request['task_id'])
        self.assertIsNotNone(lease)
        try:
            with lease.bind():
                with self.assertRaisesRegex(ValueError,'scope|Scope|confirmation'):
                    planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],kind='CONTINUE'))
                planning.stage(self.store,lease,request['id'],fixtures.envelope(self.store,request['id'],
                    kind='REQUEST_AUTHORIZATION',authorization=self.run['authorization']))
        finally:lease.release()
        run=self.f.tick()
        self.assertEqual(run['status'],'WAITING_USER')
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))[0]
        self.assertEqual(decision['kind'],'successor_authorization')
        graph.command(self.store,run['id'],'owner','answer',{
            'decision_id':decision['id'],'snapshot_digest':decision['snapshot_digest'],
            'action':'approve','authorization':run['authorization']})
        run=self.f.tick()
        self.assertTrue(run['scope_confirmed'])
        self.assertEqual(run['authorization_version'],2)
        self.assertEqual(self.store.query('SELECT COUNT(*) AS n FROM run_authorizations WHERE run_id=?',(run['id'],))[0]['n'],2)
        self.assertEqual(graph.get(self.store,original['id'])['status'],'SUCCEEDED')
