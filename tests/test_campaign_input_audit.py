import sys
import unittest
from unittest.mock import patch

from tests import campaign_dynamic_probe
with patch.dict(sys.modules,{'campaign_dynamic_probe':campaign_dynamic_probe}):
    from tests.campaign_dynamic_probe import input_sequence_invariants


class CampaignInputAuditTests(unittest.TestCase):
    def test_reply_does_not_advance_business_cursor(self):
        self.assertEqual(input_sequence_invariants({'input_sequence':0},[
            {'sequence':1,'status':'REPLIED'}]),(True,True))

    def test_business_cursor_can_follow_earlier_replies(self):
        self.assertEqual(input_sequence_invariants({'input_sequence':2},[
            {'sequence':1,'status':'REPLIED'},{'sequence':2,'status':'APPLIED'}]),(True,True))

    def test_gap_or_cursor_bound_to_reply_is_rejected(self):
        self.assertEqual(input_sequence_invariants({'input_sequence':0},[
            {'sequence':2,'status':'APPLIED'}]),(False,True))
        self.assertEqual(input_sequence_invariants({'input_sequence':1},[
            {'sequence':1,'status':'REPLIED'}]),(True,False))
