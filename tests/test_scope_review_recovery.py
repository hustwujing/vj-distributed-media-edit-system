import json
import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph
from webapp.task_leases import Coordinator,LeaseLost
from webapp.workflow_dispatch import stage_result


class ScopeReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store;self.run=self.f.review_fixture()
        authorization={**self.run['authorization'],'delivery_targets':['report','notes']}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),self.run['id']))
        self.store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(self.run['id'],))
        self.run=graph.get(self.store,self.run['id']);self.f.orchestrator.queue_review(self.run)
        self.old=self.store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final'",(self.run['id'],))[0]

    def confirm(self):
        current=graph.get(self.store,self.run['id'])
        self.f.orchestrator.decision(current,'delivery_scope_change',{'question':'Only report?','current_targets':['report','notes']})
        decision=self.store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND kind='delivery_scope_change' AND status='pending'",(self.run['id'],))[0]
        command=graph.command(self.store,self.run['id'],'owner','answer',{'decision_id':decision['id'],
            'snapshot_digest':decision['snapshot_digest'],'action':'approve',
            'authorization':{**current['authorization'],'delivery_targets':['report']}})
        run=self.f.tick()
        self.assertEqual(self.store.query('SELECT status,error_code FROM workflow_commands WHERE id=?',(command['id'],))[0],{'status':'applied','error_code':''})
        self.assertEqual(self.store.query('SELECT status FROM workflow_reviews WHERE id=?',(self.old['id'],))[0]['status'],'superseded')
        self.assertEqual(graph.work_item(run,self.old['task_id'])['status'],'cancelled')
        pending=self.store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='pending'",(run['id'],))
        self.assertEqual(len(pending),1)
        self.assertNotEqual(pending[0]['id'],self.old['id'])
        self.assertEqual(json.loads(pending[0]['snapshot_json'])['delivery_targets'],['report'])
        from webapp.review_identity import final_identity
        self.assertEqual(pending[0]['review_identity'],final_identity(self.store,run))
        self.assertEqual(run['status'],'ACTIVE')
        return run

    def test_ready_final_review_is_cancelled_and_replaced(self):
        self.confirm()

    def test_empty_delivery_targets_use_same_identity_at_review_and_publication(self):
        from webapp.review_identity import final_identity
        # Start with no pending review so an independently queued final review
        # actually freezes the empty-target fallback, rather than reusing one.
        self.store.execute("UPDATE workflow_reviews SET status='superseded' WHERE run_id=?",(self.run['id'],))
        self.store.execute("UPDATE control_jobs SET status='cancelled' WHERE run_id=? AND control_kind='REVIEW'",(self.run['id'],))
        run=graph.get(self.store,self.run['id'])
        authorization={**run['authorization'],'delivery_targets':[]}
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),run['id']))
        self.store.execute('UPDATE run_authorizations SET authorization_json=? WHERE run_id=?',(graph.encode(authorization),run['id']))
        self.f.orchestrator.queue_review(graph.get(self.store,run['id']))
        row=self.store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND status='pending'",(run['id'],))[0]
        self.assertEqual(json.loads(row['snapshot_json'])['review_criteria'],[run['goal']])
        self.assertEqual(row['review_identity'],final_identity(self.store,graph.get(self.store,run['id'])))
        self.store.execute('UPDATE runs SET graph_version=graph_version+1 WHERE id=?',(run['id'],))
        self.assertEqual(row['review_identity'],final_identity(self.store,graph.get(self.store,run['id'])))

    def test_active_final_review_is_fenced_and_cleaned_without_touching_step_review(self):
        lease=Coordinator(self.store,'old-review','test').claim(self.run['id'],'workflow',step_id=self.old['task_id'])
        self.assertIsNotNone(lease);self.addCleanup(lease.release)
        self.store.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'allocated',?)",
            (lease.attempt,self.run['id'],'old-review',self.store.now()))
        # An independent step review is not superseded by final delivery scope.
        self.f.orchestrator.queue_review(graph.get(self.store,self.run['id']),'first')
        unrelated=self.store.query("SELECT task_id FROM workflow_reviews WHERE run_id=? AND purpose='task'",(self.run['id'],))[0]['task_id']
        run=self.confirm()
        self.assertEqual(graph.work_item(run,unrelated)['status'],'ready')
        self.assertEqual(self.store.query('SELECT state FROM execution_cleanup WHERE attempt=?',(lease.attempt,))[0]['state'],'pending')
        with self.assertRaises(LeaseLost):lease.check()

    def test_staged_old_result_is_late_and_cannot_restore_old_review(self):
        lease=Coordinator(self.store,'old-review','test').claim(self.run['id'],'workflow',step_id=self.old['task_id'])
        self.assertIsNotNone(lease)
        decision={'snapshot_digest':self.old['snapshot_digest'],'verdict':'inconclusive','feedback':'old scope'}
        try:
            with lease.bind():stage_result(self.store,lease,'succeeded',{'decision':decision})
        finally:lease.release()
        run=self.confirm()
        self.assertEqual(self.store.query('SELECT status FROM task_results WHERE task_id=?',(self.old['task_id'],))[0]['status'],'late')
        with self.assertRaisesRegex(ValueError,'no longer pending'):
            self.f.orchestrator.accept_review(run,graph.work_item(run,self.old['task_id']),{'decision':decision})
