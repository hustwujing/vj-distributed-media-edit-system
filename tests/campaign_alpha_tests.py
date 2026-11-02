"""Offline contract checks for the live campaign's transparent delivery check."""
import unittest
from tests.campaign_complex_checks import has_transparent_subject


class TransparencyContractTests(unittest.TestCase):
    def test_tight_crop_with_small_transparent_corners(self):
        self.assertTrue(has_transparent_subject({'transparent_fraction':.029,'visible_fraction':.971}))

    def test_opaque_image_is_not_transparent(self):
        self.assertFalse(has_transparent_subject({'transparent_fraction':0,'visible_fraction':1}))

    def test_empty_transparent_image_is_not_a_subject(self):
        self.assertFalse(has_transparent_subject({'transparent_fraction':1,'visible_fraction':0}))

    def test_missing_evidence_does_not_pass(self):
        self.assertFalse(has_transparent_subject({}))


if __name__=='__main__':unittest.main()
