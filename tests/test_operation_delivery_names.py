import copy,json,unittest
from unittest.mock import patch
from tests import test_workflows as workflow_fixtures,test_dynamic_graph as graph_fixtures
from webapp import workflow_graph as graph
from webapp.workflow_orchestrator import Orchestrator
from webapp.workflow_agent_contracts import validate_resolved_review


class OperationDeliveryNameTests(unittest.TestCase):
    def test_review_and_publication_name_native_output_without_copying_bytes(self):
        from PIL import Image
        f=workflow_fixtures.WorkflowTests();f.setUp();self.addCleanup(f.tearDown);store=f.store
        fixture=graph_fixtures.DynamicGraphTests();fixture.f=f;fixture.store=store
        fixture.run=graph.create(store,f.project,f.turn,{'goal':'Deliver final.png','allowed_assets':['asset'],
            'allowed_operations':['image.batch_export'],'allowed_agent_profiles':[],
            'delivery_targets':['64x64 PNG'],'content_fingerprint':graph.content_fingerprint(store,f.project)})
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
        run=graph.get(store,run['id']);fixture.orchestrator.queue_review(run)
        review=store.query("SELECT * FROM workflow_reviews WHERE purpose='final'")[0];snapshot=json.loads(review['snapshot_json'])
        decision={'verdict':'pass','deliverables':[ref],'primary':ref,'feedback':'Valid output',
            'snapshot_digest':review['snapshot_digest'],'evidence':[{'criterion':'64x64 PNG','inspection_id':'a'*32,'ref':ref,'observation':'Actual64x64 PNG'}]}
        validate_resolved_review(decision,snapshot,{'a'*32:ref})
        store.execute("UPDATE workflow_reviews SET status='passed',decision_json=? WHERE id=?",(graph.encode(decision),review['id']))
        store.execute("UPDATE control_jobs SET status='succeeded' WHERE id=?",(review['task_id'],))
        objects=len(store.query('SELECT id FROM artifact_objects'))
        with patch.dict('os.environ',{'VIDEO_USE_OBJECT_BUCKET':'publication-metadata-fixture'}):fixture.orchestrator.publish(run,{'id':review['id']})
        final=graph.get(store,run['id']);self.assertEqual(final['status'],'SUCCEEDED')
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
