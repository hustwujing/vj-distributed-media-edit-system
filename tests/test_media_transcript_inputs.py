import tempfile
import unittest
from pathlib import Path
from webapp.media_transcript_inputs import resolve,materialized
from webapp.operations import validate_parameters,get
from webapp.media_handlers import Context


class TranscriptInputTests(unittest.TestCase):
    def parameters(self):
        return {'edit_plan':{'clips':[{'asset':'source','start':0,'end':2}],
                             'subtitles':{'from_transcripts':True}}}

    def test_required_transcript_role_is_validated_before_execution(self):
        inputs={'source':{'asset':'video'},'transcripts':{'step':'recognize','role':'transcript'}}
        with self.assertRaisesRegex(ValueError,'transcript_source'):
            validate_parameters('media.edit',self.parameters(),inputs)
        for role in ('transcript','transcript_source','transcript_source_t0'):
            valid={'source':inputs['source'],role:inputs['transcripts']}
            normalized=validate_parameters('media.edit',self.parameters(),valid)
            self.assertEqual(resolve('edit',normalized,valid),{('source',0):role})
        self.assertIn('transcript_binding',get('media.edit').public()['input_contract'])

    def test_multiple_sources_and_tracks_cannot_share_an_ambiguous_transcript(self):
        params={'edit_plan':{'clips':[{'asset':'source','audio_track':0},{'asset':'source','audio_track':1}],
                             'subtitles':{'from_transcripts':True}}}
        with self.assertRaisesRegex(ValueError,'transcript_source_t0'):
            resolve('edit',params,{'source':{},'transcript':{}})
        inputs={'source':{},'transcript_source_t0':{},'transcript_source_t1':{}}
        self.assertEqual(materialized('edit',params,inputs,{'source':'video','transcript_source_t0':'words0','transcript_source_t1':'words1'}),
                         {'video':{'0':'words0','1':'words1'}})
        params['edit_plan']['clips']=[{'asset':'left'},{'asset':'right'}]
        with self.assertRaises(ValueError):resolve('edit',params,{'left':{},'right':{},'transcript':{}})

    def test_worker_uses_explicit_track_binding_and_never_private_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'sources').mkdir();(root/'edit/transcripts').mkdir(parents=True)
            (root/'edit/transcripts/video.json').write_text('private cache')
            assets=[{'id':key,'filename':key+'.json','metadata':{'kind':'json'}} for key in ('video','words0','words1')]
            ctx=Context({'project_dir':str(root),'output_dir':str(root/'edit/out'),'payload':{},'assets':assets,
                         'explicit_inputs':True,'transcript_inputs':{'video':{'0':'words0','1':'words1'}}})
            self.assertEqual(ctx.transcript('video',1),root/'sources/words1.json')
            self.assertFalse(ctx.transcript('video',2).exists())
            ctx.transcript_inputs={'video':{'0':'foreign'}}
            with self.assertRaisesRegex(ValueError,'Undeclared'):ctx.transcript('video',0)

    def test_render_and_word_selection_use_the_same_role_contract(self):
        inputs={'source':{},'transcript':{}}
        self.assertEqual(resolve('render',{'segments':[{'asset':'source'}],'subtitles':'transcript'},inputs),{('source',0):'transcript'})
        with self.assertRaises(ValueError):resolve('edit',{'edit_plan':{'clips':[{'asset':'source','words':[0,3]}]}},{'source':{}})
        self.assertEqual(resolve('edit',{'edit_plan':{'clips':[{'asset':'source'}]}},{'source':{}}),{})
