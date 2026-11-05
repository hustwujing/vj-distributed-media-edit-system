import unittest

from tests.campaign_dynamic_probe import candidate_archive_disposition


class CandidateArchiveDispositionTests(unittest.TestCase):
    def test_uncommitted_failed_candidate_has_no_bytes_to_archive(self):
        self.assertEqual(candidate_archive_disposition('candidate',{}),'not_committed')

    def test_committed_artifacts_are_archived_and_required_states_cannot_be_missing(self):
        self.assertEqual(candidate_archive_disposition('candidate',{'artifact':'a'}),'archive')
        self.assertEqual(candidate_archive_disposition('accepted',{'artifact':'a'}),'archive')
        self.assertEqual(candidate_archive_disposition('accepted',{}),'missing_required_artifact')
        self.assertEqual(candidate_archive_disposition('historical_checkpoint',{}),'missing_required_artifact')


if __name__=='__main__':unittest.main()
