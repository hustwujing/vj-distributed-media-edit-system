import unittest
from webapp.workflow_agent_contracts import (
    delivery_candidate_refs,approved_delivery_refs,delivery_leaf_refs,validate_resolved_review)
from webapp.workflow_model_protocol import compile_manager


class DeliveryCandidateTests(unittest.TestCase):
    def task(self,roles,acceptance=None):
        return {'id':'render','accepted_result':list(roles.values()),'quality':{'output_roles':roles},
                'spec':{'delivery_stage':True,'acceptance':acceptance or []}}

    def test_unlabelled_preview_cannot_replace_real_video_delivery(self):
        frozen={'graph_version':0,'authorization_version':1,'snapshot_digest':'a'*64,
                'planning_request_id':'request','goal':'做一个成片，并附一张预览图',
                'authorization':{'delivery_targets':['成片和预览图']},'assets':[],
                'tasks':[],'dependencies':[],'trigger':{},'input_batch':None,'operations':[]}
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Render',
              'objective':'Render final video with preview',
              'files':[{'path':'movie.mp4','roles':['video']},{'path':'thumb.png','roles':['preview']}],
              'acceptance':[{'kind':'media','video':True,'source':'user','stage':'final'}]}
        spec=compile_manager({'kind':'APPLY_PLAN','reason':'Create outputs','plan':{'new_tasks':[task]}},frozen)['proposal']['add_tasks'][0]
        live=self.task(spec['output_roles']);live['spec']=spec
        snapshot={**frozen,'tasks':[live],'review_scope':'final','delivery_targets':['成片和预览图']}
        self.assertEqual(delivery_candidate_refs(live),{'movie.mp4'})
        self.assertEqual(approved_delivery_refs(snapshot),{'movie.mp4'})
        self.assertEqual(delivery_leaf_refs(snapshot),{'render':{'movie.mp4'}})
        decision={'verdict':'pass','deliverables':['thumb.png'],'primary':'thumb.png',
                  'evidence':[{'criterion':'成片和预览图','inspection_id':'a'*32,'ref':'thumb.png','method_use':'not_applicable'}]}
        with self.assertRaisesRegex(ValueError,'approved candidates'):
            validate_resolved_review(decision,snapshot,{'a'*32:'thumb.png'})
        decision.update(deliverables=['movie.mp4'],primary='movie.mp4')
        decision['evidence'][0]['ref']='movie.mp4'
        validate_resolved_review(decision,snapshot,{'a'*32:'movie.mp4'})

    def test_auxiliary_only_outputs_and_explicit_preview_are_preserved(self):
        self.assertEqual(delivery_candidate_refs(self.task({'preview':'timeline.png'})),{'timeline.png'})
        self.assertEqual(delivery_candidate_refs(self.task({'report':'ocr.json','preview':'preview.png'})),{'ocr.json','preview.png'})
        task=self.task({'video':'movie.mp4','preview':'thumb.png'},
                       [{'source':'user','stage':'final','kind':'image','file':'thumb.png'}])
        self.assertEqual(delivery_candidate_refs(task),{'movie.mp4','thumb.png'})
