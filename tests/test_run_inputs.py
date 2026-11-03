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
