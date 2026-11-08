import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
import unittest
import uuid
from PIL import Image
from tests import test_dynamic_graph as fixtures
from webapp import workflow_graph as graph,workflow_planning as planning
from webapp.repair_inputs import lookup,descriptors


class RepairInputTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DynamicGraphTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.store=self.f.store
        authorization={**self.f.run['authorization'],'allowed_operations':['script','media.evidence'],
                       'operation_implementations':{},'operation_policy_digests':{}}
        authorization=planning.bind_scope(self.store,authorization).model_dump()
        self.store.execute('UPDATE runs SET authorization_json=? WHERE id=?',(graph.encode(authorization),self.f.run['id']))
        self.f.tick()
        self.source_spec=fixtures.script('first',outputs=['image.png'],output_roles={'primary_image':'image.png'},
            acceptance=[{'kind':'image','width':64,'height':64},{'kind':'agent','criterion':'The corner is red'}])
        future={'id':'later-evidence','type':'operation','purpose':'evidence','name':'Inspect accepted image',
            'objective':'Inspect after the parent is accepted','operation':'media.evidence',
            'inputs':{'source':{'step':'first','role':'primary_image'}},'parameters':{'mode':'image'},
            'acceptance':[{'kind':'json','file':'evidence-report.json'},{'kind':'image','file':'preview.jpg'}]}
        self.f.propose({'proposal_id':'candidate','base_graph_version':0,'reason':'Create an image',
            'add_tasks':[self.source_spec,future],
            'add_dependencies':[{'upstream':'first','downstream':'later-evidence'}],'sealed':True})
        self.run=self.f.tick();task=self.run['tasks'][0]
        source=self.store.root/'source-candidate.png';Image.new('RGB',(64,64),'blue').save(source)
        obj=self.store.artifacts.upload(self.f.f.project,source)
        self.logical=f'edit/workflows/{self.run["id"]}/first/candidate/image.png'
        self.record={'artifact':obj['id'],'sha256':obj['sha256'],'size':obj['size'],'location':source.name}
        manifest={self.logical:self.record}
        self.store.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?,?,?)',
            (self.run['id'],'first',0,self.f.f.project,'candidate-attempt',graph.encode(manifest),self.store.now()))
        self.data={'files':[self.logical],'output_roles':{'primary_image':self.logical},
            'quality':{'technical_valid':True,'passed':True,'checks':[],'files':[{'name':self.logical,
                'kind':'image','width':64,'height':64,'sha256':obj['sha256'],'size':obj['size']}]}}
        self.f.orchestrator.accept_artifacts(self.run,task,self.data,manifest)
        self.store.execute('''INSERT INTO task_results(id,run_id,task_id,task_revision,lease_id,lease_generation,outcome,result_json,artifact_manifest,status,created_at)
            VALUES (?,?,?,0,'candidate-attempt',1,'succeeded',?,?,'committed',?)''',
            (uuid.uuid4().hex,self.run['id'],'first',graph.encode(self.data),graph.encode(manifest),self.store.now()))
        self.run=graph.get(self.store,self.run['id']);review=next(job for job in self.run['control_jobs'] if job['control_kind']=='REVIEW')
        self.f.orchestrator.accept_review(self.run,review,{'decision':{'snapshot_digest':review['snapshot_digest'],
            'verdict':'reject','evidence':[],'feedback':'The corner is blue'}})
        self.run=graph.get(self.store,self.run['id'])
        self.ref=descriptors(self.store,self.run)[0]

    def test_candidate_reference_pins_revision_object_and_hash(self):
        self.assertEqual(lookup(self.store,self.run,self.ref)['path'],self.logical)
        for changes in ({'sha256':'0'*64},{'artifact_id':'0'*32},{'revision':1},{'logical_task_id':'another'}):
            with self.assertRaises(ValueError):lookup(self.store,self.run,{**self.ref,**changes})
        with self.assertRaisesRegex(ValueError,'read-only'):
            graph.Task.model_validate(fixtures.script(inputs={'source':{'repair_candidate':self.ref}}))
        self.assertEqual(self.run['tasks'][0]['accepted_result'],[])

    def test_source_input_evidence_is_consistent_in_schema_and_authoritative_validation(self):
        from webapp.workflow_model_protocol import repair_options,ToolProtocol
        from webapp.specialist_profiles import get
        subject=next(item for item in self.run['tasks'] if item['id']=='first')
        evidence=next(item for item in self.run['tasks'] if item['id']=='later-evidence')
        source={'asset':'asset','sha256':'a'*64}
        self.store.execute('UPDATE run_tasks SET spec_json=?,bindings_json=? WHERE run_id=? AND id=?',
            (graph.encode({**subject['spec'],'inputs':{'source':{'asset':'asset'}}}),graph.encode({'source':source}),self.run['id'],'first'))
        spec={**evidence['spec'],'inputs':{'source':{'asset':'asset'}}}
        quality={'technical_valid':True,'passed':True,'verified_evidence':{'source_reference':'asset','source_sha256':'a'*64}}
        self.store.execute("UPDATE run_tasks SET spec_json=?,bindings_json=?,quality_json=?,status='succeeded' WHERE run_id=? AND id=?",
            (graph.encode(spec),graph.encode({'source':source}),graph.encode(quality),self.run['id'],'later-evidence'))
        run=graph.get(self.store,self.run['id']);frozen=planning.snapshot(self.store,run)
        option=next(item for item in repair_options(frozen) if item['action']=='review_again')
        self.assertEqual(option['eligible_evidence_tasks'],['later-evidence'])
        protocol=ToolProtocol(get('media_planner'),frozen,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertEqual(protocol.schemas['submit_typed_plan']['properties']['review_again']['properties']['first']['items']['enum'],['later-evidence'])
        proposal={'proposal_id':'source-proof','base_graph_version':run['graph_version'],'reason':'Review the existing candidate against its actual input',
                  'review_again':{'first':['later-evidence']},'replan_after':['first']}
        graph.validate_proposal(self.store,run,proposal,repair_scope='first')
        # A reused asset ID with different bytes, or forged report binding,
        # cannot make unrelated evidence eligible in either layer.
        for binding,report in [({'asset':'other','sha256':'a'*64},quality),
                               ({'asset':'asset','sha256':'b'*64},{**quality,'verified_evidence':{'source_reference':'asset','source_sha256':'b'*64}}),
                               (source,{**quality,'verified_evidence':{'source_reference':'asset','source_sha256':'b'*64}})]:
            self.store.execute('UPDATE run_tasks SET bindings_json=?,quality_json=? WHERE run_id=? AND id=?',
                               (graph.encode({'source':binding}),graph.encode(report),run['id'],'later-evidence'))
            current=graph.get(self.store,run['id'])
            with self.assertRaisesRegex(ValueError,'different frozen input'):
                graph.validate_proposal(self.store,current,proposal,repair_scope='first')
            self.assertFalse(any(item['action']=='review_again' for item in repair_options(planning.snapshot(self.store,current))))

    def test_exhausted_candidate_review_blocks_new_automatic_evidence(self):
        self.store.execute("UPDATE run_tasks SET review_again_count=2 WHERE run_id=? AND id='first'",(self.run['id'],))
        run=graph.get(self.store,self.run['id'])
        frozen=planning.snapshot(self.store,run)
        self.assertEqual(next(t['review_again_count'] for t in frozen['tasks'] if t['id']=='first'),2)
        task={'id':'unusable-evidence','type':'operation','purpose':'evidence','name':'Inspect candidate',
            'objective':'Collect facts about the existing candidate','operation':'media.evidence',
            'inputs':{'source':{'repair_candidate':self.ref}},'parameters':{'mode':'image'},
            'acceptance':[{'kind':'json','file':'evidence-report.json','required_keys':['source_sha256']},
                          {'kind':'image','file':'preview.jpg'}]}
        proposal={'proposal_id':'spent-review','base_graph_version':run['graph_version'],'reason':'More evidence',
            'add_tasks':[task],'replan_after':['unusable-evidence']}
        with self.assertRaisesRegex(ValueError,'evidence review budget exhausted'):
            graph.validate_proposal(self.store,run,proposal,repair_scope='first')
        self.assertFalse(self.store.query("SELECT id FROM run_tasks WHERE id='unusable-evidence'"))
        # Explicit user evidence work does not implicitly grant another review.
        graph.validate_proposal(self.store,run,proposal,repair_scope='first',user_revision_scope={'first'})

    def test_existing_blocked_evidence_does_not_block_new_candidate_evidence(self):
        self.assertEqual(next(t['status'] for t in self.run['tasks'] if t['id']=='later-evidence'),'blocked')
        task={'id':'fresh-evidence','type':'operation','purpose':'evidence','name':'Inspect paused candidate',
            'objective':'Collect missing facts for the existing candidate','operation':'media.evidence',
            'inputs':{'source':{'repair_candidate':self.ref}},'parameters':{'mode':'image'},
            'acceptance':[{'kind':'json','file':'evidence-report.json'},{'kind':'image','file':'preview.jpg'}]}
        proposal={'proposal_id':'repair-evidence','base_graph_version':self.run['graph_version'],
            'reason':'Collect candidate evidence without changing existing dependencies',
            'add_tasks':[task],'replan_after':['fresh-evidence']}
        graph.validate_proposal(self.store,self.run,proposal,repair_scope='first')
        invalid={**task,'inputs':{'source':{'step':'first','role':'primary_image'}}}
        with self.assertRaisesRegex(ValueError,'frozen repair candidate'):
            graph.validate_proposal(self.store,self.run,{**proposal,'add_tasks':[invalid],
                'add_dependencies':[{'upstream':'first','downstream':'fresh-evidence'}]},repair_scope='first')

    def test_opensandbox_command_materializes_registered_candidate(self):
        from webapp.opensandbox_command import OpenSandboxCommandRuntime
        from webapp.workflows import get
        task={'id':'evidence','type':'operation','purpose':'evidence','name':'Inspect candidate',
            'objective':'Collect facts about the existing candidate','operation':'media.evidence',
            'inputs':{'source':{'repair_candidate':self.ref}},'parameters':{'mode':'image'},
            'acceptance':[{'kind':'json','file':'evidence-report.json','required_keys':['source_sha256']},
                          {'kind':'image','file':'preview.jpg'}]}
        self.f.propose({'proposal_id':'collect-command','base_graph_version':1,'reason':'Only evidence is missing',
            'add_tasks':[task],'replan_after':['evidence']})
        self.f.tick()
        from webapp.task_leases import Coordinator
        lease=Coordinator(self.store,'command-evidence','test',ttl=120).claim(self.run['id'],'workflow',step_id='evidence')
        self.assertIsNotNone(lease)
        workflow=get(self.store,self.run['id'])
        runtime=OpenSandboxCommandRuntime(None,None,'command-1','command-attempt',lease)
        try:
            environment=runtime.environment(self.store,workflow,'unused')
            restored=environment['workspace']/self.logical
            self.assertTrue(restored.is_file())
            self.assertEqual(__import__('hashlib').sha256(restored.read_bytes()).hexdigest(),self.ref['sha256'])
            self.assertEqual(runtime.repair_input_paths,{self.logical})
            from webapp.workflow_inputs import staged_assets
            evidence=next(item for item in workflow['steps'] if item['id']=='evidence')
            with lease.bind():assets,roles=staged_assets(self.store,workflow,evidence,environment)
            staged=next(item for item in assets if item['id']==roles['source'])
            self.assertEqual(staged['metadata']['sha256'],self.ref['sha256'])
        finally:lease.release()

    @unittest.skipUnless(os.geteuid()==0 and shutil.which('bwrap'),'Requires privileged sandbox')
    def test_registered_evidence_reads_candidate_without_rerunning_media(self):
        from webapp.task_leases import Coordinator
        from webapp.execution_resources import IdentityPool
        from webapp.execution_runtime import TaskRuntime
        from webapp.workflow_runtime import Adapter
        from webapp.workflow_executor import execute
        task={'id':'evidence','type':'operation','purpose':'evidence','name':'Inspect candidate',
            'objective':'Collect facts about the existing candidate','operation':'media.evidence',
            'inputs':{'source':{'repair_candidate':self.ref}},'parameters':{'mode':'image'},
            'acceptance':[{'kind':'json','file':'evidence-report.json','required_keys':['source_sha256']},{'kind':'image','file':'preview.jpg'}]}
        self.f.propose({'proposal_id':'collect','base_graph_version':1,'reason':'Only evidence is missing',
            'add_tasks':[task],'replan_after':['evidence']})
        run=self.f.tick();coordinator=Coordinator(self.store,'repair-evidence','test',ttl=120)
        lease=coordinator.claim(run['id'],'workflow',step_id='evidence');self.assertIsNotNone(lease)
        sockets=self.store.root/'sockets';sockets.mkdir()
        pool=IdentityPool(self.store.root/'identities',start=645000,size=2)
        host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=Adapter(self.store,'test',sockets)))
        lease.runtime_factory=lambda:TaskRuntime(lease,pool)
        try:
            with lease.bind():execute(host,lease)
        finally:
            if lease.runtime:lease.runtime.close()
            lease.release()
        run=self.f.tick();evidence=next(item for item in run['tasks'] if item['id']=='evidence')
        self.assertEqual(evidence['status'],'succeeded',evidence)
        self.assertEqual(evidence['quality']['verified_evidence']['source_sha256'],self.ref['sha256'])
        self.assertEqual(next(item for item in run['tasks'] if item['id']=='first')['accepted_result'],[])
        self.assertFalse(self.store.query("SELECT id FROM execution_attempts WHERE step_id='first'"))
        subject=next(item for item in run['tasks'] if item['id']=='first')
        previous_quality={**subject['quality'],'agent_review_passed':True,'requires_review':True,'requires_agent':True}
        spec={**subject['spec'],'acceptance':[*subject['spec']['acceptance'],{'kind':'review','criterion':'User confirms'}]}
        self.store.execute('UPDATE run_tasks SET quality_json=?,spec_json=? WHERE run_id=? AND id=?',
            (graph.encode(previous_quality),graph.encode(spec),run['id'],'first'))
        self.f.propose({'proposal_id':'review-with-evidence','base_graph_version':2,'reason':'Recheck the same candidate',
            'review_again':{'first':['evidence']},'sealed':True})
        run=self.f.tick();subject=next(item for item in run['tasks'] if item['id']=='first')
        self.assertEqual((subject['revision'],subject['review_again_count'],subject['status']),(0,1,'reviewing'))
        self.assertNotIn('agent_review_passed',subject['quality'])
        self.assertEqual(subject['quality'],{key:value for key,value in previous_quality.items() if key!='agent_review_passed'})
        self.assertFalse(self.store.query("SELECT id FROM workflow_decisions WHERE kind='quality' AND status='pending'"))
        reviews=self.store.query("SELECT snapshot_json FROM workflow_reviews WHERE subject_task='first' AND status='pending'")
        self.assertEqual(json.loads(reviews[0]['snapshot_json'])['evidence_tasks'][0]['id'],'evidence')
