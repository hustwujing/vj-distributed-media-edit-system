import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from webapp.workflow_agent_contracts import validate_temporal_evidence
from webapp.workflow_graph import TemporalSemantics
from webapp.workflow_agents import inspect_media


class TemporalEvidenceTests(unittest.TestCase):
    def snapshot(self):
        return {'review_scope':'task','tasks':[{'spec':{'semantics':{'temporal':[TemporalSemantics(action='continuous motion').model_dump()]}},
            'candidate_result':['clip.mp4'],'accepted_result':[],
            'quality':{'output_roles':{'video':'clip.mp4'}}}]}

    def record(self,times):
        return {'id':'a'*32,'reference':'clip.mp4','evidence_json':json.dumps({'coverage':'contact_sheet',
            'duration_seconds':8,'source_frame_interval_seconds':.04,'frame_timestamps':times})}

    def test_sparse_stills_cannot_prove_continuous_motion(self):
        decision={'verdict':'pass','evidence':[{'inspection_id':'a'*32}]}
        with self.assertRaisesRegex(ValueError,'sparse'):
            validate_temporal_evidence(decision,self.snapshot(),[self.record([0,4,7.5])])
        validate_temporal_evidence(decision,self.snapshot(),[self.record([i*.5 for i in range(16)])])
        with self.assertRaisesRegex(ValueError,'timestamped'):
            validate_temporal_evidence(decision,self.snapshot(),[{**self.record([0,7.5]),'id':'b'*32}])

    def test_final_review_only_requires_evidence_for_selected_delivery_tasks(self):
        snapshot=self.snapshot();snapshot['review_scope']='final'
        selected=snapshot['tasks'][0];selected['spec']['delivery_stage']=True
        selected['accepted_result']=['clip.mp4'];selected['candidate_result']=[]
        unselected={**selected,'accepted_result':['alternate.mp4'],
            'quality':{'output_roles':{'video':'alternate.mp4'}}}
        snapshot['tasks'].insert(0,unselected)
        decision={'verdict':'pass','deliverables':['clip.mp4'],'evidence':[{'inspection_id':'a'*32}]}
        records=[self.record([i*.5 for i in range(16)])]
        validate_temporal_evidence(decision,snapshot,records)
        with self.assertRaisesRegex(ValueError,'timestamped'):
            validate_temporal_evidence({**decision,'deliverables':['clip.mp4','alternate.mp4']},snapshot,records)
        with self.assertRaisesRegex(ValueError,'sparse'):
            validate_temporal_evidence(decision,snapshot,[self.record([0,4,7.5])])

    def test_selecting_a_report_cannot_bypass_a_delivery_tasks_temporal_gate(self):
        snapshot=self.snapshot();snapshot['review_scope']='final'
        task=snapshot['tasks'][0];task['spec']['delivery_stage']=True
        task['accepted_result']=['clip.mp4','report.json'];task['candidate_result']=[]
        decision={'verdict':'pass','deliverables':['report.json'],'evidence':[{'inspection_id':'a'*32}]}
        with self.assertRaisesRegex(ValueError,'verified video candidates'):
            validate_temporal_evidence(decision,snapshot,[self.record([i*.5 for i in range(16)])])

    def test_each_selected_video_is_checked_even_with_a_primary_video_role(self):
        snapshot=self.snapshot();snapshot['review_scope']='final'
        task=snapshot['tasks'][0];task['spec']['delivery_stage']=True
        task['candidate_result']=[];task['accepted_result']=['clip.mp4','alternate.mp4']
        task['quality']['files']=[{'name':name,'video':True} for name in task['accepted_result']]
        first=self.record([i*.5 for i in range(16)])
        alternate={**first,'id':'b'*32,'reference':'alternate.mp4'}
        decision={'verdict':'pass','deliverables':['alternate.mp4'],'evidence':[{'inspection_id':'b'*32}]}
        validate_temporal_evidence(decision,snapshot,[alternate])
        with self.assertRaisesRegex(ValueError,'timestamped'):
            validate_temporal_evidence({**decision,'deliverables':task['accepted_result']},snapshot,[alternate])
        validate_temporal_evidence({**decision,'deliverables':task['accepted_result'],
            'evidence':[{'inspection_id':'a'*32},{'inspection_id':'b'*32}]},snapshot,[first,alternate])

    def test_identical_verified_outputs_share_content_evidence_but_distinct_bytes_do_not(self):
        snapshot=self.snapshot();task=snapshot['tasks'][0]
        task['candidate_result']=['clip.mp4','alias.mp4']
        task['quality']['files']=[{'name':name,'video':True,'sha256':'c'*64} for name in task['candidate_result']]
        record={**self.record([i*.5 for i in range(16)]),'reference':'alias.mp4'}
        decision={'verdict':'pass','evidence':[{'inspection_id':record['id']}]}
        validate_temporal_evidence(decision,snapshot,[record])
        task['quality']['files'][1]['sha256']='d'*64
        with self.assertRaisesRegex(ValueError,'timestamped'):validate_temporal_evidence(decision,snapshot,[record])

    def test_evidence_aliases_require_authoritative_hashes_and_full_sampling(self):
        snapshot=self.snapshot();task=snapshot['tasks'][0]
        task['candidate_result']=['clip.mp4','alias.mp4']
        task['quality']['files']=[{'name':name,'video':True,'sha256':'c'*64} for name in task['candidate_result']]
        record={**self.record([0,4,7.5]),'reference':'alias.mp4'}
        decision={'verdict':'pass','evidence':[{'inspection_id':record['id']}]}
        with self.assertRaisesRegex(ValueError,'sparse'):validate_temporal_evidence(decision,snapshot,[record])
        record={**self.record([i*.5 for i in range(16)]),'reference':'unfrozen.mp4'}
        with self.assertRaisesRegex(ValueError,'timestamped'):validate_temporal_evidence(decision,snapshot,[record])

    def test_registered_evidence_for_an_alias_keeps_source_hash_validation(self):
        snapshot=self.snapshot();task=snapshot['tasks'][0]
        task['candidate_result']=['clip.mp4','alias.mp4']
        task['quality']['files']=[{'name':name,'video':True,'sha256':'c'*64} for name in task['candidate_result']]
        proof={**json.loads(self.record([i*.5 for i in range(16)])['evidence_json']),
            'source_reference':'alias.mp4','source_sha256':'c'*64}
        record={'id':'a'*32,'reference':'evidence.json','evidence_json':json.dumps({'verified_media':proof})}
        decision={'verdict':'pass','evidence':[{'inspection_id':record['id']}]}
        validate_temporal_evidence(decision,snapshot,[record])
        record['evidence_json']=json.dumps({'verified_media':{**proof,'source_sha256':'d'*64}})
        with self.assertRaisesRegex(ValueError,'timestamped'):validate_temporal_evidence(decision,snapshot,[record])

    def test_actual_video_window_has_bounded_timestamped_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'clip.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=25:duration=3',
                '-c:v','libx264','-threads','1',str(source)],check=True)
            env={'assets':[{'id':'asset','filename':'clip.mp4'}]}
            host=SimpleNamespace(workflow_runner=SimpleNamespace(adapter=SimpleNamespace(environment=lambda _:env)))
            lease=SimpleNamespace(runtime=object())
            run={'id':'run','project_id':'project','tasks':[],'control_jobs':[],'goal':'motion','status':'ACTIVE',
                'graph_version':1,'sealed':True,'terminal_reason':'','created_at':0,'updated_at':0}
            def command(env,gateway,argv):return [str(source) if item=='/workspace/sources/clip.mp4' else item for item in argv]
            with patch('webapp.agent_sandbox.command',side_effect=command):
                evidence=inspect_media(host,lease,run,{}, {'ref':'asset','path':source,'media':True,
                    'still_image':False,'offset_seconds':0,'window_seconds':3},remaining_images=1)
            self.assertEqual(evidence['coverage'],'contact_sheet')
            self.assertGreaterEqual(len(evidence['frame_timestamps']),8)
            self.assertLessEqual(len(evidence['frame_timestamps']),16)
            self.assertEqual(evidence['duration_seconds'],3)
            self.assertTrue(evidence['image'].startswith('data:image/jpeg;base64,'))
            snapshot=self.snapshot();snapshot['tasks'][0]['spec']['semantics']['temporal'][0]['required_coverage']=.9
            record={'id':'a'*32,'reference':'clip.mp4','evidence_json':json.dumps({key:evidence[key] for key in
                ('coverage','duration_seconds','source_frame_interval_seconds','frame_timestamps')})}
            validate_temporal_evidence({'verdict':'pass','evidence':[{'inspection_id':'a'*32}]},snapshot,[record])
