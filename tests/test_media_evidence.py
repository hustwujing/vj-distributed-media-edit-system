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
