import json,unittest,uuid
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,run_inputs


class FinalReviewInputIdentityTests(unittest.TestCase):
    def test_applied_user_requirements_create_a_new_final_review_context(self):
        f=fixtures.DynamicGraphTests();f.setUp();self.addCleanup(f.tearDown)
        store=f.store;run=f.review_fixture()
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        f.orchestrator.queue_review(graph.get(store,run['id']))
        old=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        store.execute("UPDATE workflow_reviews SET status='passed',decision_json=? WHERE id=?",
            (graph.encode({'deliverables':['edit/report.txt'],'primary':'edit/report.txt'}),old['id']))
        store.execute("UPDATE control_jobs SET status='succeeded' WHERE id=?",(old['task_id'],))
        received=run_inputs.accept(store,run['id'],'owner',{'request_id':uuid.uuid4().hex,'content':'Keep the same file, and verify the newly clarified contents.'})
        store.execute("UPDATE run_inputs SET status='APPLIED' WHERE id=?",(received['id'],))
        store.execute("UPDATE runs SET input_barrier_sequence=NULL,sealed=1,graph_version=graph_version+1,status='ACTIVE' WHERE id=?",(run['id'],))
        f.orchestrator.queue_review(graph.get(store,run['id']))
        reviews=store.query("SELECT * FROM workflow_reviews WHERE purpose='final' ORDER BY created_at")
        self.assertEqual(len(reviews),2)
        self.assertNotEqual(reviews[0]['review_identity'],reviews[1]['review_identity'])
        self.assertEqual(reviews[0]['snapshot_json'],old['snapshot_json'])
        self.assertEqual(reviews[1]['status'],'pending')
        self.assertEqual(json.loads(reviews[1]['snapshot_json'])['user_changes'][0]['content'],'Keep the same file, and verify the newly clarified contents.')
