import unittest
from tests.campaign_suite import order_cases


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
