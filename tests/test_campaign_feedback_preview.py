import tempfile
from pathlib import Path
import unittest
from PIL import Image,ImageDraw,ImageFilter
from tests.campaign_feedback_preview import check


class LocalFeedbackPreviewTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.original=Path(self.folder.name)/'source.png';self.candidate=Path(self.folder.name)/'candidate.png'
        self.image=Image.new('RGBA',(32,32),(40,80,120,0))
        alpha=Image.new('L',(32,32));ImageDraw.Draw(alpha).rectangle((8,8,23,23),fill=255)
        self.image.putalpha(alpha);self.image.save(self.original)

    def test_local_patch_has_explicit_coordinates_and_preserves_other_pixels(self):
        candidate=self.image.copy();candidate.putpixel((8,8),(40,80,120,0));candidate.save(self.candidate)
        result=check(self.original,self.candidate,'alpha_erode_patch')
        self.assertTrue(result['passed']);self.assertEqual(result['patch_box'],(8,8,9,9))
        self.assertEqual(result['expected_changed_pixels'],1)
        self.assertLessEqual(result['expected_changed_fraction'],.01)

    def test_global_erosion_and_unchanged_output_do_not_satisfy_local_feedback(self):
        self.image.save(self.candidate);self.assertFalse(check(self.original,self.candidate,'alpha_erode_patch')['passed'])
        candidate=self.image.copy();candidate.putalpha(candidate.getchannel('A').filter(ImageFilter.MinFilter(3)));candidate.save(self.candidate)
        self.assertFalse(check(self.original,self.candidate,'alpha_erode_patch')['passed'])

    def test_alpha_patch_does_not_authorize_rgb_changes(self):
        candidate=self.image.copy();candidate.putpixel((8,8),(0,0,0,0));candidate.save(self.candidate)
        self.assertFalse(check(self.original,self.candidate,'alpha_erode_patch')['passed'])
