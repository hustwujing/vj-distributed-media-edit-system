import copy,json,unittest
from unittest.mock import patch
from tests import test_workflows as workflow_fixtures,test_dynamic_graph as graph_fixtures
from webapp import workflow_graph as graph
from webapp.workflow_orchestrator import Orchestrator
from webapp.workflow_agent_contracts import validate_resolved_review


class OperationDeliveryNameTests(unittest.TestCase):
    def test_review_and_publication_name_native_output_without_copying_bytes(self):
        self.publish_native()

    def test_removed_report_role_does_not_block_actual_final_publication(self):
        self.publish_native(drop_report=True)

    def test_same_graph_version_cannot_reuse_review_before_parameter_change(self):
        self.publish_native(change_after_review=True)

    def publish_native(self,drop_report=False,change_after_review=False):
        from PIL import Image
        f=workflow_fixtures.WorkflowTests();f.setUp();self.addCleanup(f.tearDown);store=f.store
        fixture=graph_fixtures.DynamicGraphTests();fixture.f=f;fixture.store=store
        fixture.run=graph.create(store,f.project,f.turn,{'goal':'Deliver final.png and JSON report' if drop_report else 'Deliver final.png','allowed_assets':['asset'],
            'allowed_operations':['image.batch_export'],'allowed_agent_profiles':[],
            'delivery_targets':['final.png','JSON report'] if drop_report else ['64x64 PNG'],'content_fingerprint':graph.content_fingerprint(store,f.project)})
        fixture.orchestrator=Orchestrator(store,'test-native-publication');fixture.tick()
        fixture.propose({'proposal_id':'native','base_graph_version':0,'reason':'Registered image export',
            'add_tasks':[{'id':'export','type':'operation','operation':'image.batch_export','name':'Export','objective':'Resize source',
                'inputs':{'source':{'asset':'asset'}},'parameters':{'parameters':{'width':64,'height':64,'format':'png','fit':'contain'}},
                'acceptance':[{'kind':'image','width':64,'height':64}],'delivery_stage':True}],'sealed':True})
        run=fixture.tick();source=store.root/'accepted.png';Image.new('RGB',(64,64),'blue').save(source)
        record=store.artifacts.upload(f.project,source);ref='edit/runs/'+run['id']+'/export/output/image.png'
        manifest={ref:{'artifact':record['id'],'sha256':record['sha256'],'size':record['size']}}
        store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (run['id'],'export',0,f.project,'accepted',graph.encode(manifest),store.now()))
        store.execute("UPDATE run_tasks SET status='succeeded',accepted_result_json=?,quality_json=? WHERE run_id=? AND id='export'",
            (graph.encode([ref]),graph.encode({'technical_valid':True,'passed':True,'output_roles':{'primary_image':ref}}),run['id']))
        run=graph.get(store,run['id'])
        if drop_report:
            fixture.orchestrator.decision(run,'delivery_scope_change',{'question':'Only deliver the PNG, remove the JSON report?','current_targets':run['authorization']['delivery_targets']})
            approval=store.query("SELECT * FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],))[0]
            fixture.orchestrator.answer(graph.get(store,run['id']),{'decision_id':approval['id'],'snapshot_digest':approval['snapshot_digest'],
                'action':'approve','authorization':{**run['authorization'],'delivery_targets':['final.png']}})
            run=graph.get(store,run['id'])
        else:fixture.orchestrator.queue_review(run)
        review=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0];snapshot=json.loads(review['snapshot_json'])
        if drop_report:self.assertTrue(snapshot['effective_delivery_scope']['confirmed'])
        decision={'verdict':'pass','deliverables':[ref],'primary':ref,'feedback':'Valid output',
            'snapshot_digest':review['snapshot_digest'],'evidence':[{'criterion':criterion,'inspection_id':'a'*32,'ref':ref,'method_use':'not_applicable','observation':'Actual64x64 PNG satisfies requested delivery'}
                for criterion in snapshot['review_criteria']]}
        validate_resolved_review(decision,snapshot,{'a'*32:ref})
        store.execute("UPDATE workflow_reviews SET status='passed',decision_json=? WHERE id=?",(graph.encode(decision),review['id']))
        store.execute("UPDATE control_jobs SET status='succeeded' WHERE id=?",(review['task_id'],))
        if change_after_review:
            from tests.run_input_fixture import accept_change
            message=accept_change(store,run['id'],'owner',{'request_id':'f'*32,'content':'保持PNG交付，把图片改为128×128并使用蓝色背景。'})
            # Boundary after a Manager CONTINUE consumed the input without
            # revising the DAG: the independent content review must still run.
            store.execute("UPDATE run_inputs SET status='APPLIED' WHERE id=?",(message['id'],))
            store.execute('UPDATE runs SET input_barrier_sequence=NULL WHERE id=?',(run['id'],))
        objects=len(store.query('SELECT id FROM artifact_objects'))
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):fixture.orchestrator.publish(run,{'id':review['id']})
        final=graph.get(store,run['id'])
        if change_after_review:
            self.assertEqual(final['graph_version'],run['graph_version']);self.assertIsNone(final['delivered_at'])
            pending=store.query("SELECT snapshot_json FROM workflow_reviews WHERE run_id=? AND purpose='final' AND status='pending'",(run['id'],))
            self.assertEqual(len(pending),1);self.assertIn('128×128',pending[0]['snapshot_json']);return
        self.assertEqual(final['status'],'SUCCEEDED')
        self.assertEqual(final['delivery'][0]['name'],'final.png');self.assertEqual(final['delivery'][0]['sha256'],record['sha256'])
        self.assertEqual(len(store.query('SELECT id FROM artifact_objects')),objects)
        self.assertEqual(final['tasks'][0]['accepted_result'],[ref]);self.assertEqual(len(final['tasks']),1)

    def snapshot(self):
        return {'goal':'Deliver final.png','tasks':[{'id':'export','spec':{'type':'operation','delivery_stage':True},
            'accepted_result':['edit/image.png'],'quality':{'output_roles':{'primary_image':'edit/image.png'}}}]}

    def test_mapping_requires_one_requested_name_and_one_eligible_same_format_candidate(self):
        from webapp.workflow_agent_contracts import delivery_publication_names
        value=self.snapshot();self.assertEqual(delivery_publication_names(value),{'edit/image.png':'final.png'})
        for change in ('second_candidate','second_name','different_format','agent','excluded'):
            altered=copy.deepcopy(value)
            if change=='second_candidate':altered['tasks'][0]['accepted_result'].append('edit/other.png');altered['tasks'][0]['quality']['output_roles']['other']='edit/other.png'
            elif change=='second_name':altered['goal']='Deliver final.png and cover.png'
            elif change=='different_format':altered['goal']='Deliver final.jpg'
            elif change=='agent':altered['tasks'][0]['spec']['type']='agent'
            else:altered['goal']='Deliver final.png; image.png not for delivery'
            with self.subTest(change=change):self.assertEqual(delivery_publication_names(altered),{})

    def test_applied_final_filename_change_replaces_original_name_without_losing_other_outputs(self):
        from webapp.workflow_model_protocol import required_delivery_names
        from webapp.workflow_agent_contracts import delivery_publication_names
        for subject in ('最终视频文件名','最终音频名称','交付图片文件名','最终文档名称','final video filename'):
            frozen={'goal':'最终视频命名为 original.mp4。另交付 captions.srt。',
                    'authorization':{'delivery_targets':['renamed.mp4','captions.srt']},
                    'user_changes':[{'sequence':1,'content':subject+' 改为 renamed.mp4，其他要求保持不变。'}]}
            # Use the same explicit delivery vocabulary in the initial scope;
            # unrelated subtitle delivery must survive the filename change.
            frozen['goal']='交付 original.mp4 和 captions.srt。'
            self.assertEqual(required_delivery_names(frozen),{'renamed.mp4','captions.srt'})
        frozen={'goal':'最终视频命名为 original.mp4。先给方案。','authorization':{'delivery_targets':['renamed.mp4']},
                'user_changes':[{'sequence':1,'content':'谢谢，另外请把最终视频文件名改为 renamed.mp4，其他要求保持不变。'}],
                'tasks':[{'id':'speed','spec':{'type':'operation','delivery_stage':True},'accepted_result':['edit/final.mp4'],
                          'quality':{'output_roles':{'video':'edit/final.mp4'}}}]}
        self.assertEqual(required_delivery_names(frozen),{'renamed.mp4'})
        self.assertEqual(delivery_publication_names(frozen),{'edit/final.mp4':'renamed.mp4'})
        frozen['user_changes'].append({'sequence':2,'content':'最终视频文件名改成 latest.mp4。'})
        frozen['authorization']['delivery_targets']=['latest.mp4']
        self.assertEqual(required_delivery_names(frozen),{'latest.mp4'})
        frozen['goal']='交付 main.mp4 和 trailer.mp4。'
        frozen['user_changes']=[{'content':'最终视频文件名改为 latest.mp4。'}]
        self.assertTrue({'main.mp4','trailer.mp4'}<=required_delivery_names(frozen),'Ambiguous rename cannot remove a sibling delivery')
        frozen['user_changes']=[{'content':'main.mp4 改为 latest.mp4。'}]
        self.assertEqual(required_delivery_names(frozen),{'latest.mp4','trailer.mp4'})
