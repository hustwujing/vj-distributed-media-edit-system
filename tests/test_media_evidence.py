import tempfile
import unittest
from pathlib import Path

from PIL import Image

from webapp.media_evidence import alpha_evidence,audio_evidence,normalize_text,text_matches
from webapp.quality_policy import Acceptance


class TextEvidenceTests(unittest.TestCase):
    def test_match_modes_are_distinct(self):
        self.assertFalse(text_matches('first\nsecond',['first second'],'contains'))
        self.assertTrue(text_matches('first\nsecond',['first second'],'token_sequence'))
        self.assertTrue(text_matches(' first  \r\n second ',['first\nsecond'],'normalized_text'))
        self.assertFalse(text_matches('first second',['first\nsecond'],'exact_text'))

    def test_unicode_normalization_is_deterministic(self):
        self.assertEqual(normalize_text('e\u0301\r\n  x  '),'\u00e9\nx')


class AlphaEvidenceTests(unittest.TestCase):
    def test_alpha_levels_have_disjoint_transparent_partial_opaque_measurements(self):
        for alpha,transparent,visible,opaque,partial in [
                (0,1,0,0,0),(1,0,0,0,1),(127,0,0,0,1),
                (128,0,1,0,1),(254,0,1,0,1),(255,0,1,1,0)]:
            with self.subTest(alpha=alpha):
                value=alpha_evidence(Image.new('RGBA',(8,8),(10,20,30,alpha)))
                self.assertEqual((value['transparent_fraction'],value['visible_fraction'],
                    value['opaque_fraction'],value['partial_alpha_fraction']),(transparent,visible,opaque,partial))
                self.assertEqual(value['transparent_fraction']+value['opaque_fraction']+value['partial_alpha_fraction'],1)
        mixed=Image.new('RGBA',(6,1));mixed.putdata([(10,20,30,a) for a in (0,1,127,128,254,255)])
        value=alpha_evidence(mixed)
        self.assertAlmostEqual(value['transparent_fraction'],1/6)
        self.assertAlmostEqual(value['partial_alpha_fraction'],4/6)
        self.assertAlmostEqual(value['opaque_fraction'],1/6)
        self.assertEqual(value['visible_fraction'],.5)

    def test_real_quality_checker_rejects_false_transparency_and_empty_subjects(self):
        import json,subprocess,sys
        checker=Path(__file__).resolve().parents[1]/'webapp/quality_check.py'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            images={'almost_opaque':Image.new('RGBA',(8,8),(10,20,30,254)),
                    'almost_empty':Image.new('RGBA',(8,8),(10,20,30,1)),
                    'empty':Image.new('RGBA',(8,8),(0,0,0,0)),
                    'cutout':Image.new('RGBA',(8,8),(0,0,0,0))}
            images['cutout'].paste((10,20,30,255),(0,0,4,8))
            for name,image in images.items():
                image.save(root/(name+'.png'))
                rules=[{'kind':'image','transparent_subject':True},
                       {'kind':'image','min_transparent_fraction':.5},
                       {'kind':'image','min_visible_fraction':.5}]
                expected={'almost_opaque':[False,False,True],'almost_empty':[False,False,False],
                          'empty':[False,True,False],'cutout':[True,True,True]}[name]
                for rule,passed in zip(rules,expected):
                    with self.subTest(name=name,rule=rule):
                        result=subprocess.run([sys.executable,str(checker)],cwd=root,
                            input=json.dumps({'files':[name+'.png'],'acceptance':[rule]}),
                            capture_output=True,text=True,check=True)
                        self.assertEqual(json.loads(result.stdout)['passed'],passed,result.stdout)

    def test_independent_campaign_image_checker_uses_the_same_alpha_thresholds(self):
        # Load only this pure image function, without importing the live
        # campaign module's deployment configuration or database environment.
        import ast
        source=Path(__file__).with_name('campaign_probe.py')
        node=next(item for item in ast.parse(source.read_text()).body if isinstance(item,ast.FunctionDef) and item.name=='image_info')
        scope={};exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),scope)
        with tempfile.TemporaryDirectory() as directory:
            for alpha,transparent,visible in ((0,1,0),(1,0,0),(127,0,0),(128,0,1),(254,0,1),(255,0,1)):
                path=Path(directory)/f'{alpha}.png';Image.new('RGBA',(8,8),(10,20,30,alpha)).save(path)
                value=scope['image_info'](path)
                self.assertEqual((value['transparent_fraction'],value['visible_fraction']),(transparent,visible))

    def test_reports_visible_components_and_holes(self):
        image=Image.new('RGBA',(20,20),(0,0,0,0))
        for y in range(2,18):
            for x in range(2,18):image.putpixel((x,y),(255,255,255,255))
        for y in range(8,12):
            for x in range(8,12):image.putpixel((x,y),(255,255,255,0))
        result=alpha_evidence(image)
        self.assertEqual(result['alpha_components'],1)
        self.assertGreater(result['alpha_hole_fraction'],0)
        self.assertAlmostEqual(result['visible_fraction'],240/400)

    def test_transparent_subject_rejects_empty_and_accepts_tight_crop(self):
        empty=Image.new('RGBA',(8,8),(0,0,0,0));tight=Image.new('RGBA',(8,8),(255,0,0,255));tight.putpixel((0,0),(0,0,0,0))
        self.assertEqual(alpha_evidence(empty)['visible_fraction'],0)
        evidence=alpha_evidence(tight)
        self.assertGreater(evidence['visible_fraction'],0);self.assertGreater(evidence['transparent_fraction'],0)


class AcceptanceContractTests(unittest.TestCase):
    def test_audio_state_is_not_track_presence(self):
        value=Acceptance(kind='media',audio_track_present=True,audio_silent=True,channel_layout='stereo')
        self.assertTrue(value.audio_track_present)
        self.assertTrue(value.audio_silent)
        with self.assertRaises(ValueError):Acceptance(kind='media',audio=True)

    def test_suggestion_cannot_block(self):
        with self.assertRaises(ValueError):Acceptance(kind='image',source='suggestion',severity='required')
        self.assertEqual(Acceptance(kind='image',source='suggestion',severity='signal').source,'suggestion')

    def test_exact_text_has_one_expected_value(self):
        with self.assertRaises(ValueError):Acceptance(kind='text',contains=['a','b'],text_match='exact_text')


class AudioEvidenceTests(unittest.TestCase):
    def test_silence_acceptance_distinguishes_absent_track_and_all_tracks_silent(self):
        import json,subprocess,sys
        checker=Path(__file__).resolve().parents[1]/'webapp/quality_check.py'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=s=64x48:r=10:d=1',
                '-c:v','libx264','-threads','1',str(root/'silent.mp4')],check=True)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(root/'silent.mp4'),
                '-f','lavfi','-i','anullsrc=r=48000:cl=mono:d=1','-f','lavfi','-i','sine=frequency=440:duration=1',
                '-map','0:v','-map','1:a','-map','2:a','-c:v','copy','-c:a','aac',str(root/'two.mp4')],check=True)
            for name,rule,expected in [('silent.mp4',{'audio_silent':True},True),
                ('silent.mp4',{'audio_silent':True,'audio_track_present':True},False),
                ('silent.mp4',{'audio_silent':False},False),('silent.mp4',{'max_loudness_lufs':-10},False),
                ('two.mp4',{'audio_silent':True},False),('two.mp4',{'audio_silent':False},True)]:
                with self.subTest(file=name,rule=rule):
                    result=subprocess.run([sys.executable,str(checker)],cwd=root,
                        input=json.dumps({'files':[name],'acceptance':[{'kind':'media',**rule}]}),capture_output=True,text=True,check=True)
                    self.assertEqual(json.loads(result.stdout)['passed'],expected,result.stdout)

    def test_plain_pcm_channel_count_satisfies_layout_but_does_not_guess_surround(self):
        import json,subprocess,sys,wave
        from webapp.media_evidence import audio_channel_layout
        self.assertIsNone(audio_channel_layout({'channels':6})['channel_layout'])
        self.assertEqual(audio_channel_layout({'channels':6,'channel_layout':'5.1(side)'})['channel_layout'],'5.1(side)')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for channels,layout in ((1,'mono'),(2,'stereo')):
                path=root/f'{layout}.wav'
                with wave.open(str(path),'wb') as wav:
                    wav.setnchannels(channels);wav.setsampwidth(2);wav.setframerate(16000)
                    wav.writeframes(b'\x00\x01'*channels*16000)
                for expected,passed in ((layout,True),('stereo' if channels==1 else 'mono',False)):
                    request={'files':[path.name],'acceptance':[{'kind':'media','channel_layout':expected}]}
                    checked=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'webapp/quality_check.py')],
                        cwd=root,input=json.dumps(request),capture_output=True,text=True,check=True)
                    report=json.loads(checked.stdout)
                    self.assertEqual(report['passed'],passed)
                    self.assertEqual(report['files'][0]['audio_tracks'][0]['channels'],channels)
                    self.assertEqual(report['files'][0]['channel_layout_source'],'channel_count')

    def test_missing_measurement_is_not_silence(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch('webapp.media_evidence.subprocess.run',return_value=SimpleNamespace(returncode=0,stderr=b'')):
            with self.assertRaisesRegex(ValueError,'incomplete'):audio_evidence('unread.wav')

    def test_sound_after_two_minutes_is_not_reported_as_whole_file_silence(self):
        import subprocess,json
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'late-audio.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                r'aevalsrc=if(lt(t\,121)\,0\,sin(2*PI*440*t)):s=8000:d=122',str(path)],check=True)
            sampled=audio_evidence(path,sample_seconds=120)
            complete=audio_evidence(path)
            self.assertTrue(sampled['audio_silent']);self.assertEqual(sampled['measurement_scope'],'sampled')
            self.assertFalse(complete['audio_silent']);self.assertEqual(complete['measurement_scope'],'full')
            json.dumps(complete,allow_nan=False)

    def test_silence_and_tone_are_objective_separate_measurements(self):
        import subprocess
        from webapp.media_evidence import dominant_frequency
        with tempfile.TemporaryDirectory() as root:
            tone=Path(root)/'tone.wav';silence=Path(root)/'silence.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1',str(tone)],check=True)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1',str(silence)],check=True)
            self.assertFalse(audio_evidence(tone,sample_seconds=1)['audio_silent'])
            self.assertTrue(audio_evidence(silence,sample_seconds=1)['audio_silent'])
            self.assertAlmostEqual(dominant_frequency(tone,sample_seconds=1),440,delta=8)

    def test_audio_evidence_supports_bounded_offsets(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'levels.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'sine=frequency=440:duration=2','-af','volume=0.5',str(path)],check=True)
            measured=audio_evidence(path,sample_seconds=.5,offset_seconds=1)
            self.assertEqual(measured['measurement_scope'],'sampled')
            self.assertEqual(measured['measurement_offset_seconds'],1)

    def test_audio_envelope_and_comparison_are_bounded_measurements(self):
        import subprocess,json
        from webapp.media_evidence_operation import collect
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'fade.wav';reference=root/'tone.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'sine=frequency=440:duration=2','-af','afade=t=out:st=1:d=1',str(source)],check=True)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'sine=frequency=440:duration=2',str(reference)],check=True)
            envelope=root/'envelope';envelope.mkdir();collect(source,'audio',envelope,
                {'mode':'audio_envelope','offset':1,'window_seconds':1})
            report=json.loads((envelope/'evidence-report.json').read_text())
            self.assertGreater(report['envelope'][0]['mean_volume_db'],report['envelope'][-1]['mean_volume_db'])
            compared=root/'compared';compared.mkdir();collect(source,'audio',compared,
                {'mode':'audio_compare','offset':0,'window_seconds':1},reference=reference)
            comparison=json.loads((compared/'evidence-report.json').read_text())['comparison']
            self.assertGreater(comparison['normalized_correlation'],.95)
            self.assertEqual(comparison['interpretation'],'bounded_signal_similarity_not_source_separation_or_identity')

    def test_audio_comparison_bounds_decoded_samples_despite_packet_timestamps(self):
        import subprocess
        from webapp.media_evidence_operation import audio_comparison
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'timestamped.mka'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                'sine=frequency=440:sample_rate=48000:duration=9',
                '-af','asetpts=PTS*0.99','-c:a','aac',str(source)],check=True)
            for offset,window in ((0,8),(1,.5)):
                with self.subTest(offset=offset,window=window):
                    measured=audio_comparison(source,source,offset,window)
                    self.assertEqual(measured['samples'],int(window*8000))
                    self.assertEqual(measured['window_seconds'],window)
                    self.assertAlmostEqual(measured['normalized_correlation'],1)

    def test_audio_comparison_rejects_out_of_bounds_windows_before_decoding(self):
        from unittest.mock import patch
        from webapp.media_evidence_operation import audio_comparison
        with patch('webapp.media_evidence_operation.subprocess.run') as run:
            for offset,window in ((0,9),(0,0),(-1,1),(float('nan'),1),(0,float('inf'))):
                with self.subTest(offset=offset,window=window),self.assertRaisesRegex(ValueError,'window exceeds'):
                    audio_comparison('unused','unused',offset,window)
            run.assert_not_called()

    def test_audio_comparison_aligns_trimmed_source_and_measures_stable_tones(self):
        import json,wave
        import numpy as np
        from webapp.media_evidence_operation import collect,audio_comparison
        from webapp.operation_parameters import EvidenceParameters
        rate=8000;t=np.arange(rate*2)/rate
        values=np.rint(np.concatenate([.2*np.sin(2*np.pi*220*t),.2*np.sin(2*np.pi*660*t)])*32767).astype('<i2')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);reference=root/'reference.wav';source=root/'trimmed.wav'
            for path,data in ((reference,values),(source,values[rate*2:rate*3])):
                with wave.open(str(path),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate);out.writeframes(data.tobytes())
            wrong=audio_comparison(source,reference,0,1)
            self.assertLess(abs(wrong['normalized_correlation']),.05)
            self.assertAlmostEqual(wrong['reference_tone_hz'],220,delta=2)
            output=root/'evidence';output.mkdir()
            params=EvidenceParameters(mode='audio_compare',offset=0,reference_offset=2,window_seconds=1).model_dump()
            collect(source,'audio',output,params,reference=reference)
            report=json.loads((output/'evidence-report.json').read_text());correct=report['comparison']
            self.assertEqual(report['parameters'],params)
            self.assertEqual(correct['reference_offset_seconds'],2)
            self.assertGreater(correct['normalized_correlation'],.99999)
            self.assertLessEqual(correct['normalized_correlation'],1)
            self.assertAlmostEqual(correct['source_tone_hz'],660,delta=2)
            self.assertAlmostEqual(correct['reference_tone_hz'],660,delta=2)
            self.assertAlmostEqual(correct['tone_difference_hz'],0,delta=.1)
            with self.assertRaises(ValueError):EvidenceParameters(mode='video_window',reference_offset=2)

    def test_stable_tone_measurement_does_not_invent_pitch_for_noise_or_silence(self):
        import numpy as np
        from webapp.media_evidence import _stable_tone_frequency
        self.assertIsNone(_stable_tone_frequency(np.zeros(8000),8000))
        self.assertIsNone(_stable_tone_frequency(np.random.default_rng(7).normal(size=8000),8000))


class MaskChangeTests(unittest.TestCase):
    def test_change_limit_uses_subject_area_not_empty_canvas(self):
        from webapp.media_evidence import mask_change_evidence
        with tempfile.TemporaryDirectory() as root:
            before=Path(root)/'before.png';after=Path(root)/'after.png'
            image=Image.new('L',(100,100),0);image.paste(255,(45,45,55,55));image.save(before)
            image.putpixel((50,50),0);image.save(after)
            self.assertEqual(mask_change_evidence(before,after)['changed_fraction'],.01)

    def test_full_resolution_delta_and_geometry_are_not_guessed_from_topology(self):
        from PIL import Image
        from webapp.media_evidence import mask_change_evidence
        with tempfile.TemporaryDirectory() as root:
            before=Path(root)/'before.png';after=Path(root)/'after.png'
            image=Image.new('L',(10,10),255);image.save(before)
            image.putpixel((5,5),0);image.save(after)
            self.assertEqual(mask_change_evidence(before,after)['changed_fraction'],.01)
            image.resize((20,20)).save(after)
            with self.assertRaisesRegex(ValueError,'geometry'):mask_change_evidence(before,after)

    def test_color_photo_without_alpha_is_not_a_mask(self):
        from PIL import Image
        from webapp.media_evidence import mask_change_evidence
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'photo.png';Image.new('RGB',(10,10),'red').save(source)
            with self.assertRaisesRegex(ValueError,'grayscale'):mask_change_evidence(source,source)


class MediaSemanticsTests(unittest.TestCase):
    def test_explicit_subject_relations_override_defaults_without_conflicts(self):
        from webapp.workflow_graph import SubjectSemantics
        subject=SubjectSemantics(mode='subject_cutout',include_relations=['held'])
        self.assertIn('held',subject.include_relations);self.assertNotIn('held',subject.exclude_relations)
        foreground=SubjectSemantics(mode='foreground_cutout',exclude_relations=['carried'])
        self.assertIn('carried',foreground.exclude_relations);self.assertNotIn('carried',foreground.include_relations)
        with self.assertRaises(ValueError):SubjectSemantics(mode='subject_cutout',include_relations=['held'],exclude_relations=['held'])

    def test_video_evidence_measures_static_structure_and_binds_the_reference(self):
        import subprocess,hashlib,json
        from webapp.media_evidence_operation import collect
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'clip.mp4';reference=root/'reference.png';output=root/'evidence';output.mkdir()
            Image.new('RGB',(128,96),'blue').save(reference)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','color=c=blue:s=128x96:r=25:d=2',
                            '-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(source)],check=True)
            collect(source,'video',output,{'mode':'video_window','window_seconds':2},reference=reference)
            report=json.loads((output/'evidence-report.json').read_text());structure=report['structure']
            self.assertEqual(report['reference_sha256'],hashlib.sha256(reference.read_bytes()).hexdigest())
            self.assertEqual(structure['unique_sample_frames'],1)
            self.assertEqual(max(structure['adjacent_luma_mae']),0)
            self.assertLess(max(structure['reference_luma_mae']),.02)
            self.assertEqual(structure['frame_count'],len(report['frame_timestamps']))
            self.assertEqual(structure['interpretation'],'structural_change_not_identity_verdict')
            self.assertEqual(report['preview_sha256'],hashlib.sha256((output/'preview.jpg').read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
