import unittest
from unittest.mock import Mock,patch
from webapp.sandbox_backends import AmbiguousProvisioning,DuplicateProvisioning
from tests.run_opensandbox_recovery_live import create


class RecoverySetupTests(unittest.TestCase):
    def test_unknown_create_waits_for_receipt_without_resubmitting(self):
        client=Mock();client.create.side_effect=AmbiguousProvisioning('unknown')
        client.list_by_attempt.side_effect=[[],[{'id':'owned-sandbox'}]];owned={}
        with patch('tests.run_opensandbox_recovery_live.time.sleep'):
            self.assertEqual(create(client,None,'attempt',owned),'owned-sandbox')
        client.create.assert_called_once();self.assertEqual(owned,{'attempt':'owned-sandbox'})

    def test_duplicate_setup_retains_attempt_for_cleanup(self):
        client=Mock();client.create.side_effect=AmbiguousProvisioning('unknown')
        client.list_by_attempt.return_value=[{'id':'one'},{'id':'two'}];owned={}
        with self.assertRaises(DuplicateProvisioning):create(client,None,'attempt',owned)
        self.assertEqual(owned,{'attempt':''});client.create.assert_called_once()

    def test_unresolved_setup_retains_attempt_and_has_a_deadline(self):
        client=Mock();client.create.side_effect=AmbiguousProvisioning('unknown')
        client.list_by_attempt.return_value=[];owned={}
        with patch('tests.run_opensandbox_recovery_live.time.monotonic',side_effect=[0,1,31]),\
             patch('tests.run_opensandbox_recovery_live.time.sleep'),self.assertRaises(AmbiguousProvisioning):
            create(client,None,'attempt',owned)
        self.assertEqual(owned,{'attempt':''});client.create.assert_called_once()
