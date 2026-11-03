import json
import unittest
import uuid
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph


class RunFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=self.f.review_fixture(user_review=True)
        self.f.orchestrator.decision(self.run,'quality',{'task_id':'first','revision':0})
        self.decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]

    def answer(self,action='revise',**extra):
        return {'decision_id':self.decision['id'],'snapshot_digest':self.decision['snapshot_digest'],
                'action':action,'feedback':'Make the report more concise',**extra}

    def submit(self,body=None,key=None):
        return graph.command(self.store,self.run['id'],'owner','answer',body or self.answer(),request_id=key or uuid.uuid4().hex)

    def test_feedback_uses_the_inbox_and_creates_an_immutable_revision(self):
        key=uuid.uuid4().hex;self.submit(key=key);self.submit(key=key)
        run=self.f.tick()
        self.assertEqual(len(self.store.query('SELECT * FROM run_inputs')),1)
        self.assertEqual(self.store.query('SELECT source FROM run_inputs')[0]['source'],'quality_feedback')
        self.assertEqual(run['authorization_version'],1)
        request=self.store.query("SELECT * FROM planner_runs WHERE status='pending'")[0]
        batch=json.loads(request['snapshot_json'])['input_batch']
        self.assertEqual(batch['inputs'][0]['content'],'Make the report more concise')
        self.assertEqual(batch['inputs'][0]['context']['allowed_revision_tasks'],['first'])
        task=run['tasks'][0]
        revision={**task['spec'],'objective':'Write the same report concisely','argv':['python','-c','print("concise report")']}
        self.f.propose({'proposal_id':'user-revision','base_graph_version':run['graph_version'],
            'reason':'Apply explicit feedback','revise_tasks':[revision],'sealed':True})
        run=self.f.tick();task=run['tasks'][0]
        self.assertEqual((task['revision'],task['user_revision_count'],task['repair_count']),(1,1,0))
        self.assertEqual(task['candidate_result'],[])
        self.assertEqual(len(self.store.query('SELECT revision FROM workflow_task_revisions WHERE task_id=?',('first',))),2)
        self.assertIsNone(run['input_barrier_sequence'])
        self.assertEqual(self.store.query('SELECT status FROM run_inputs')[0]['status'],'APPLIED')

    def test_invalid_attachment_rolls_back_decision_task_and_input_together(self):
        self.submit(self.answer(attachments=['foreign-asset']))
        run=self.f.tick()
        self.assertEqual(run['tasks'][0]['status'],'reviewing')
        self.assertFalse(self.store.query('SELECT id FROM run_inputs'))
        self.assertEqual(self.store.query('SELECT status FROM workflow_decisions')[0]['status'],'pending')
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands')[0]['status'],'rejected')

    def test_empty_or_stale_feedback_cannot_consume_a_decision(self):
        with self.assertRaises(ValueError):graph.DecisionAnswer.model_validate(self.answer(feedback='  '))
        self.submit(self.answer(snapshot_digest='0'*64));self.f.tick()
        self.assertFalse(self.store.query('SELECT id FROM run_inputs'))
        self.assertEqual(self.store.query('SELECT status FROM workflow_decisions')[0]['status'],'pending')

    def test_user_cannot_approve_a_reviewer_failure(self):
        self.store.execute("UPDATE workflow_decisions SET status='superseded'")
        self.store.execute('UPDATE run_tasks SET quality_json=? WHERE run_id=?',
            (graph.encode({'technical_valid':True,'requires_agent':True,'agent_review_passed':False}),self.run['id']))
        run=graph.get(self.store,self.run['id'])
        self.f.orchestrator.decision(run,'quality_feedback',{'task_id':'first','revision':0})
        self.decision=self.store.query("SELECT * FROM workflow_decisions WHERE status='pending'")[0]
        self.submit(self.answer('approve'));run=self.f.tick()
        self.assertEqual(run['tasks'][0]['accepted_result'],[])
        self.assertEqual(self.store.query('SELECT status FROM workflow_commands')[0]['status'],'rejected')
