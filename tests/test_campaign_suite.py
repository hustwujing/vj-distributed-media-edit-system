import os
from unittest import TestCase, mock

from tests import campaign_suite


class _Parser:
    def error(self, message):
        raise ValueError(message)


class CampaignSuiteTests(TestCase):
    def test_risk_order_runs_complex_cases_before_simple_cases_stably(self):
        cases = [
            {'id': 'S01', 'complexity': 'simple'},
            {'id': 'C01', 'complexity': 'complex'},
            {'id': 'D01', 'complexity': 'dynamic_protocol'},
            {'id': 'C02', 'complexity': 'complex'},
            {'id': 'R01', 'complexity': 'intervention'},
        ]
        self.assertEqual(
            [case['id'] for case in campaign_suite.order_cases(cases, 'risk')],
            ['C01', 'C02', 'R01', 'D01', 'S01'],
        )
        self.assertEqual(campaign_suite.order_cases(cases, 'manifest'), cases)

    def test_unknown_complexity_is_retained_at_the_end(self):
        cases = [
            {'id': 'X01', 'complexity': 'future'},
            {'id': 'S01', 'complexity': 'simple'},
        ]
        self.assertEqual(
            [case['id'] for case in campaign_suite.order_cases(cases, 'risk')],
            ['S01', 'X01'],
        )

    def test_deployment_roots_must_match(self):
        with mock.patch.dict(os.environ, {'DAS_DEPLOY_ROOT': '/tmp/another-cluster'}):
            with self.assertRaisesRegex(ValueError, 'must identify the same isolated deployment'):
                campaign_suite.validate_deployment_roots(_Parser())

    def test_matching_deployment_roots_are_accepted(self):
        with mock.patch.dict(os.environ, {'DAS_DEPLOY_ROOT': str(campaign_suite.DEPLOY_ROOT)}):
            campaign_suite.validate_deployment_roots(_Parser())
