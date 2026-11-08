import json,unittest,uuid
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,run_inputs


class FinalReviewInputIdentityTests(unittest.TestCase):
    def test_planner_summary_cannot_erase_structural_user_requirements(self):
        from webapp.review_identity import final_criteria
        from webapp.workflow_agent_contracts import validate_resolved_review
        goal='添加可编辑调整图层。保留参数，可以关闭调整，原图保持不变。'
        criteria=final_criteria({'goal':goal,'authorization':{'delivery_targets':['调整后的 PNG']}},[])
        self.assertEqual(criteria[0],'调整后的 PNG');self.assertTrue(any(goal in item for item in criteria))
        identifier='a'*32
        decision={'verdict':'pass','evidence':[{'criterion':criteria[0],'inspection_id':identifier,'ref':'image.png'}],
                  'deliverables':['image.png'],'primary':'image.png'}
        with self.assertRaisesRegex(ValueError,'every frozen criterion'):
            validate_resolved_review(decision,{'review_scope':'final','review_criteria':criteria},{identifier:'image.png'})
        long_goal='保留所有可编辑图层与源素材；'*500
        bounded=final_criteria({'goal':long_goal,'authorization':{'delivery_targets':['项目文件']}},[])
        self.assertLess(len(bounded),100);self.assertTrue(all(len(item)<=2000 for item in bounded))
        self.assertEqual(''.join(item.split('：',1)[1] for item in bounded[1:]),long_goal)
        multi=final_criteria({'goal':'生成字幕。\n参数：\n- 使用大写\n- 保留音轨',
                             'authorization':{'delivery_targets':['MP4','SRT']}},[])
        self.assertTrue(any('使用大写' in item and '保留音轨' not in item for item in multi))
        self.assertTrue(any('保留音轨' in item and '使用大写' not in item for item in multi))

    def test_applied_user_requirements_create_a_new_final_review_context(self):
        f=fixtures.DynamicGraphTests();f.setUp();self.addCleanup(f.tearDown)
        store=f.store;run=f.review_fixture()
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=candidate_result_json WHERE run_id=?",(run['id'],))
        f.orchestrator.queue_review(graph.get(store,run['id']))
        old=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0]
        self.assertTrue(any(run['goal'] in item for item in json.loads(old['snapshot_json'])['review_criteria']))
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
