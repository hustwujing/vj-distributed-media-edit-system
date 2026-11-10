"""Review follow-up cases through executor receipts and controller recovery."""
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import requests

from tests import test_run_recovery as fixtures
from webapp import workflow_graph as graph
from webapp.task_leases import Coordinator,TaskQuotaReached
from webapp.workflow_executor import execute
from webapp.workflow_runtime import StepFailure


class ReviewRecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RunRecoveryTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store=self.f.store;self.run=self.f.run

    def replace_task(self,spec):
        from webapp.specialist_profiles import get
        value=graph.Task.model_validate({'basic_reason':'Recovery protocol fixture; no content acceptance bypass',**spec}).model_dump()
        profile=get(value['profile']).snapshot() if value['type']=='agent' else {}
        phase='submit' if (value.get('operation') or '').startswith('video.') else 'local'
        self.store.execute("UPDATE run_tasks SET type=?,spec_json=?,profile_json=?,phase=?,status='ready' WHERE run_id=? AND id='render'",
            (value['type'],graph.encode(value),graph.encode(profile),phase,self.run['id']))
        self.run=graph.get(self.store,self.run['id'])

    def claim(self):
        lease=Coordinator(self.store,'review-regression','test').claim(self.run['id'],'workflow',step_id='render')
        self.assertIsNotNone(lease)
        return lease

    def test_agent_connection_error_is_staged_then_safely_retried(self):
        self.replace_task({'id':'render','type':'agent','profile':'editing_implementer','name':'report',
            'objective':'Write report','outputs':['report.txt'],'output_roles':{'report':'report.txt'},
            'acceptance':[{'kind':'file'}],'basic_reason':'Local report'})
        harness=SimpleNamespace(run=Mock(side_effect=requests.ConnectionError('temporary model disconnect')))
        host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=SimpleNamespace()))
        lease=self.claim()
        try:
            with lease.bind(),patch('webapp.profile_harnesses.get',return_value=harness):execute(host,lease)
        finally:lease.release()
        receipt=json.loads(self.store.query('SELECT result_json FROM task_results WHERE run_id=?',(self.run['id'],))[0]['result_json'])
        self.assertEqual(receipt['error_code'],'agent_model_transport_failed');self.assertTrue(receipt['transient'])
        self.f.f.orchestrator.accept_results(graph.get(self.store,self.run['id']))
        self.assertEqual(graph.get(self.store,self.run['id'])['tasks'][0]['status'],'retry_wait')
        self.assertEqual(harness.run.call_count,1)

    def test_local_operation_wrapped_import_error_is_retryable(self):
        self.assert_import_retry(StepFailure({'error_type':'ImportError','message':'runtime import failed'}))

    def test_local_operation_direct_import_error_is_retryable(self):
        self.assert_import_retry(ImportError('runtime import failed'))

    def assert_import_retry(self,error):
        self.replace_task({'id':'render','type':'operation','operation':'media.evidence','name':'evidence','objective':'Collect evidence',
            'inputs':{'source':{'asset':'asset'}},'parameters':{'mode':'image'},'acceptance':[{'kind':'file'}]})
        adapter=SimpleNamespace(fixed_media=Mock(side_effect=error))
        host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=adapter))
        lease=self.claim()
        try:
            with lease.bind():execute(host,lease)
        finally:lease.release()
        self.f.f.orchestrator.accept_results(graph.get(self.store,self.run['id']))
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual((task['status'],task['error_code']),('retry_wait','execution_failed'))
        self.assertEqual(adapter.fixed_media.call_count,1)
        detail=json.loads(self.store.query('SELECT record FROM workflow_diagnostics WHERE workflow=?',(self.run['id'],))[0]['record'])
        self.assertEqual((detail['error_type'],detail['classification']),('ImportError','environment'))

    def test_external_submit_import_error_is_never_automatically_retried(self):
        self.replace_task({'id':'render','type':'operation','operation':'video.generate_from_image','name':'video','objective':'Generate video',
            'inputs':{'source':{'asset':'asset'}},'parameters':{'prompt':'Move','width':1280,'height':720,'duration':5},
            'acceptance':[{'kind':'file'}]})
        adapter=SimpleNamespace(submit=Mock(side_effect=StepFailure({'error_type':'ImportError','message':'runtime import failed'})))
        host=SimpleNamespace(store=self.store,stop=threading.Event(),workflow_runner=SimpleNamespace(adapter=adapter))
        lease=self.claim()
        try:
            with lease.bind():execute(host,lease)
        finally:lease.release()
        self.f.f.orchestrator.accept_results(graph.get(self.store,self.run['id']))
        task=graph.get(self.store,self.run['id'])['tasks'][0]
        self.assertEqual(task['status'],'paused')
        self.assertEqual(adapter.submit.call_count,1)

    def test_hex_log_fragments_do_not_become_http_authorization_or_rate_limits(self):
        from webapp.execution_contracts import classify_error
        for digits in ('401','403','429'):
            self.assertEqual(classify_error({'log_tail':'sha256='+'a'*30+digits+'b'*31},'local'),'execution')
        self.assertEqual(classify_error({'http_status':403},'local'),'authorization')
        self.assertEqual(classify_error({'http_status':429},'local'),'rate_limit')

    def test_sixth_model_reservation_respects_five_call_authorization(self):
        from webapp.workflow_agents import model_budget
        from webapp.usage_protocol import Receipt
        run={**self.run,'authorization':{**self.run['authorization'],'max_model_calls':5}}
        lease=self.claim()
        try:
            for _ in range(5):
                observer=Receipt();observer.usage={'input_tokens':100,'output_tokens':20,'total_tokens':120}
                with lease.bind(),model_budget(lease,run,120,observer,model_call_limit=40):pass
            with lease.bind(),self.assertRaises(TaskQuotaReached) as blocked,model_budget(lease,run,120,Receipt(),model_call_limit=40):
                self.fail('Sixth request must not be sent')
            self.assertEqual(blocked.exception.diagnostic['category'],'model')
        finally:lease.release()
        self.assertEqual(self.store.query("SELECT used FROM execution_budgets WHERE run_id=? AND category='model'",(run['id'],))[0]['used'],5)
        self.assertEqual(len(self.store.query('SELECT id FROM workflow_model_reservations WHERE run_id=?',(run['id'],))),5)

    def test_daily_quota_failure_exposes_actual_counts_and_reset_guidance(self):
        from webapp.workflow_api import public
        self.f.f.orchestrator.task_failure(self.run,self.run['tasks'][0],{
            'error_code':'daily_model_quota_exhausted','quota_used':37,'quota_limit':37,
            'quota_reset_at':self.store.now()+7200})
        snapshot=public(self.store,graph.get(self.store,self.run['id']))
        failure=next(row for row in snapshot['failure_details'] if row['code']=='daily_model_quota_exhausted')
        self.assertIn('37/37',failure['message'])
        self.assertIn('小时后恢复',failure['message'])
        self.assertEqual(snapshot['blocking_action']['code'],'daily_model_quota_exhausted')
