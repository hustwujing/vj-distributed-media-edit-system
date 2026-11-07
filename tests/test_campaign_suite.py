from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from tests import campaign_suite
from tests.campaign_suite import order_cases,run_browser


class _Parser:
    def error(self,message):raise ValueError(message)


class CampaignSuiteTests(unittest.TestCase):
    def test_risk_order_runs_complex_cases_before_simple_cases_stably(self):
        cases=[
            {'id':'S01','complexity':'simple'},
            {'id':'C01','complexity':'complex'},
            {'id':'D01','complexity':'dynamic_protocol'},
            {'id':'C02','complexity':'complex'},
            {'id':'R01','complexity':'intervention'},
        ]
        self.assertEqual([case['id'] for case in order_cases(cases,'risk')],
                         ['C01','C02','R01','D01','S01'])
        self.assertEqual(order_cases(cases,'manifest'),cases)

    def test_unknown_complexity_is_retained_at_the_end(self):
        cases=[{'id':'X01','complexity':'future'},{'id':'S01','complexity':'simple'}]
        self.assertEqual([case['id'] for case in order_cases(cases,'risk')],['S01','X01'])

    def test_deployment_roots_must_match(self):
        with patch.dict(os.environ,{'DAS_DEPLOY_ROOT':'/tmp/another-cluster'}):
            with self.assertRaisesRegex(ValueError,'must identify the same isolated deployment'):
                campaign_suite.validate_deployment_roots(_Parser())

    def test_matching_deployment_roots_are_accepted(self):
        with patch.dict(os.environ,{'DAS_DEPLOY_ROOT':str(campaign_suite.DEPLOY_ROOT)}):
            campaign_suite.validate_deployment_roots(_Parser())

    def test_parent_interrupt_stops_child_and_recovers_new_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'.cluster/campaigns/dynamic-plan/runs';runs.mkdir(parents=True)
            class Process:
                calls=0;terminated=False
                def wait(self,timeout=None):
                    self.calls+=1
                    if self.calls==1:
                        attempt=runs/'S01/attempt-001';attempt.mkdir(parents=True);(attempt/'state.json').write_text('{}')
                        raise KeyboardInterrupt
                    return 0
                def poll(self):return None
                def terminate(self):self.terminated=True
                def kill(self):raise AssertionError('Graceful child termination should finish')
            process=Process()
            with patch('tests.campaign_suite.ROOT',root),patch('tests.campaign_suite.DEPLOY_ROOT',root/'.cluster'), \
                 patch('tests.campaign_suite.subprocess.Popen',return_value=process), \
                 patch('tests.campaign_suite.interrupt_attempt',return_value=True) as recover:
                self.assertEqual(run_browser([{'id':'S01'}],'http://127.0.0.1/distributed-agent-system',1),130)
            self.assertTrue(process.terminated);self.assertTrue((root/'.cluster/campaigns/dynamic-plan/pause-new-cases').exists())
            recover.assert_called_once()
