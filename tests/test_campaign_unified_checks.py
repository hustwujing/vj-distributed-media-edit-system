import unittest
from tests.campaign_unified_checks import check


class ExactDeliveryTests(unittest.TestCase):
    def state(self,names=('deliver.mp4',),sha='a'*64):
        return {'case':'D41','fixture_hashes':{'motion_reference.mp4':'a'*64},
            'artifacts':[{'name':name,'sha256':sha} for name in names]}

    def test_exact_selected_copy_passes(self):
        checks,errors=check(self.state(),[],[],None,None)
        self.assertEqual(errors,[]);self.assertTrue(checks['delivery_matches_frozen_input_sha256'])

    def test_additional_retained_candidates_are_not_a_passing_delivery(self):
        _,errors=check(self.state(('deliver.mp4','candidate.mp4','primary.mp4')),[],[],None,None)
        self.assertTrue(any('Only deliver.mp4' in error for error in errors))

    def test_changed_bytes_fail_even_when_filename_and_format_match(self):
        _,errors=check(self.state(sha='b'*64),[],[],None,None)
        self.assertTrue(any('exact frozen' in error for error in errors))
