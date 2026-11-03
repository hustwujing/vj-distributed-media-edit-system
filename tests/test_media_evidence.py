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


if __name__=='__main__':unittest.main()
