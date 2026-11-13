import unittest
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph


class FinalInconclusiveBudgetTests(unittest.TestCase):
    def test_second_inconclusive_result_stops_automatic_evidence_planning(self):
        f=fixtures.DynamicGraphTests();f.setUp();self.addCleanup(f.tearDown)
        store=f.store;run=f.review_fixture()
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        for index in range(2):
            if index:
                # A completed evidence plan adds genuinely new evidence, so the
                # second review has a new identity (not a duplicate verdict draw).
                store.execute("UPDATE planner_runs SET status='applied' WHERE run_id=? AND status='pending'",(run['id'],))
                store.execute("UPDATE control_jobs SET status='succeeded' WHERE run_id=? AND control_kind='PLAN'",(run['id'],))
                spec=graph.Task.model_validate({'id':'evidence','type':'operation','operation':'media.evidence','purpose':'evidence',
                    'name':'extra evidence','objective':'Resolve review uncertainty','inputs':{'source':{'asset':'asset'}},
                    'parameters':{'mode':'image'},'acceptance':[{'kind':'file'}],'basic_reason':'Fixed evidence fixture'}).model_dump()
                proof={'verified_evidence':{'source_sha256':'a'*64,'preview_sha256':'b'*64,'kind':'image',
                    'parameters':{'mode':'image'},'implementation_id':'fixed_media@1'}}
                store.execute("INSERT INTO run_tasks(run_id,id,type,purpose,spec_json,status,created_graph_version,quality_json,created_at,updated_at) VALUES (?,'evidence','operation','evidence',?,'succeeded',2,?,?,?)",
                    (run['id'],graph.encode(spec),graph.encode(proof),store.now(),store.now()))
                store.execute('UPDATE runs SET sealed=1,graph_version=2 WHERE id=?',(run['id'],))
            current=graph.get(store,run['id']);f.orchestrator.queue_review(current)
            row=store.query("SELECT * FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='pending'",(run['id'],))[0]
            current=graph.get(store,run['id']);task=graph.work_item(current,row['task_id'])
            f.orchestrator.accept_review(current,task,{'decision':{'snapshot_digest':row['snapshot_digest'],
                'verdict':'inconclusive','evidence':[],'deliverables':[],'primary':'','feedback':'Evidence still insufficient',
                'missing_evidence':[{'fact':'Inspect one additional image fact','mode':'image'}]}})
            if not index:self.assertEqual(graph.get(store,run['id'])['terminal_reason'],'evidence_planning')
        current=graph.get(store,run['id'])
        self.assertEqual((current['status'],current['terminal_reason']),('WAITING_USER','review_incomplete'))
        self.assertFalse(store.query("SELECT id FROM planner_runs WHERE run_id=? AND status='pending'",(run['id'],)))
        self.assertEqual(graph.work_item(current,'first')['accepted_result'],['edit/report.txt'])
        self.assertEqual(graph.work_item(current,'first')['revision'],0)
        self.assertIsNone(current['delivered_at'])
        f.tick()
        self.assertEqual(graph.get(store,run['id'])['status'],'WAITING_USER')
