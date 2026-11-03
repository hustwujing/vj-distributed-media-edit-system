"""Protocol selection and recovery through the real persistent Runner."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from pydantic import BaseModel,ConfigDict
from webapp import operations,workflows
from webapp.operation_lifecycle import (
    Implementation,Lifecycle,LifecycleError,Protocol,RecoveryAction,
    IMPLEMENTATIONS,register,
)


class Parameters(BaseModel):
    model_config=ConfigDict(extra='forbid')


class LifecycleProtocolTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workflows import WorkflowTests
        self.fixture=WorkflowTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store

    def install(self,lifecycle,*,records_calls=False,handler=None):
        identifier='protocol_test@2';operation='test.protocol'
        phase={Lifecycle.LOCAL:'local',Lifecycle.SYNC:'sync',Lifecycle.ASYNC:'submit'}[lifecycle]
        implementation=Implementation(identifier,lifecycle,
            handlers={phase:handler} if handler else {},
            execute_method='local',records_calls=records_calls)
        register(implementation)
        self.addCleanup(IMPLEMENTATIONS.pop,identifier,None)
        metadata={} if lifecycle==Lifecycle.LOCAL else {'external_side_effects':True,'budget_group':operation,'concurrency_group':'protocol-test','provider_identity':'protocol-test','route_adapter':'openai_images' if lifecycle==Lifecycle.SYNC else 'generation','cleanup_category':'unqueryable_sync' if lifecycle==Lifecycle.SYNC else 'generation_protocol'}
        operations.register(operations.Operation(operation,'test','protocol_test',input_kinds=(),
            implementation_id=identifier,retry_safe=True,**metadata),operations.OperationContract(Parameters))
        self.addCleanup(operations.REGISTRY.pop,operation,None)
        self.addCleanup(operations._CONTRACTS.pop,operation,None)
        self.fixture.submit([{'id':'test','name':'test','kind':'operation','operation':operation,
            'goal':'Produce the approved image','acceptance':[{'kind':'image','width':8}],
            'retry_safe':True,'on_failure':'pause'}])
        return IMPLEMENTATIONS[identifier]

    def step(self):return self.fixture.get()['steps'][0]

    def change(self,**values):
        workflows.change(self.store,self.fixture.get()['id'],'test',**values)

    def recover(self):self.fixture.runner.recover(self.fixture.turn)

    def save_receipt(self,state='succeeded',result=None):
        from webapp.provider_calls import intent
        w=self.fixture.get();s=w['steps'][0]
        key=intent(self.store,w,s,'test-provider','test.protocol')
        self.store.execute('UPDATE workflow_provider_calls SET state=?,result=? WHERE request_key=?',
            (state,json.dumps(result or []),key))

    def test_registration_rejects_unknown_lifecycle_and_misspelled_handler(self):
        for implementation in (
            Implementation('invalid@1','remote_callback',handlers={'submit':lambda *a:None}),
            Implementation('invalid@1',Lifecycle.LOCAL,'local',handlers={'sync':lambda *a:None}),
            Implementation('invalid@1',Lifecycle.ASYNC,collect_method=''),
        ):
            with self.subTest(implementation=implementation):
                with self.assertRaises(ValueError):register(implementation)
                self.assertNotIn('invalid@1',IMPLEMENTATIONS)

    def test_protocol_rejects_missing_recovery_or_unimplemented_runner_phase(self):
        with self.assertRaises(ValueError):
            Protocol(('local',),{'pending':'local','verifying':'validate'},{'local':RecoveryAction.RETRY_LOCAL})
        with self.assertRaises(ValueError):
            Protocol(('callback',),{'pending':'callback','verifying':'validate'},
                     {'callback':RecoveryAction.QUERY,'validate':RecoveryAction.VERIFY})

    def test_unknown_protocol_never_falls_through_to_submit(self):
        implementation=self.install(Lifecycle.ASYNC)
        with patch.dict(IMPLEMENTATIONS,{implementation.id:replace(implementation,lifecycle='remote_callback')}):
            self.fixture.tick()
            self.assertEqual(self.step()['error_code'],'lifecycle')
            self.assertEqual(self.fixture.get()['status'],'paused')
            self.assertEqual(self.step()['attempts'],0)
            self.assertEqual(self.fixture.adapter.calls,[])
            self.assertFalse(workflows.public(self.fixture.get())['resumable'])

    def test_takeover_rejects_unknown_protocol_without_external_call(self):
        implementation=self.install(Lifecycle.ASYNC)
        self.change(status='submitting',attempts=1)
        with patch.dict(IMPLEMENTATIONS,{implementation.id:replace(implementation,lifecycle='remote_callback')}):
            self.recover()
        self.assertEqual(self.step()['error_code'],'lifecycle')
        self.assertEqual(self.fixture.adapter.calls,[])

    def test_sync_cannot_poll_an_external_id_even_after_manual_resume(self):
        self.install(Lifecycle.SYNC)
        self.change(status='waiting',external_id='foreign-task')
        self.fixture.tick()
        self.assertEqual(self.step()['error_code'],'lifecycle')
        self.assertEqual(self.fixture.adapter.calls,[])
        self.change(status='paused',error_code='poll')
        self.assertFalse(workflows.public(self.fixture.get())['resumable'])
        with self.assertRaises(ValueError):workflows.resume(self.store,self.fixture.turn)

    def test_async_missing_handle_is_rejected_instead_of_resubmitted(self):
        self.install(Lifecycle.ASYNC)
        self.change(status='waiting')
        self.recover()
        self.assertEqual(self.step()['error_code'],'lifecycle')
        self.assertEqual(self.fixture.adapter.calls,[])

    def test_new_sync_implementation_recovers_durable_result_without_recalling(self):
        implementation=self.install(Lifecycle.SYNC,records_calls=True)
        self.change(status='submitting',attempts=1)
        files=['edit/workflows/test/saved.png']
        self.save_receipt(result=files)
        with patch.object(Implementation,'recovery',autospec=True,side_effect=Implementation.recovery) as recovery:
            self.recover()
            self.assertTrue(any(call.args[1]=='sync' for call in recovery.call_args_list))
        self.assertEqual(self.step()['status'],'verifying')
        self.assertEqual(self.step()['candidate'],files)
        self.fixture.tick()
        self.assertEqual(self.step()['status'],'succeeded')
        self.assertEqual(self.fixture.adapter.calls,[('publish',tuple(files))])
        advertised=operations.get('test.protocol').public()['recovery_by_phase']
        self.assertEqual(set(advertised),{'sync','validate'})
        self.assertEqual(advertised['sync'],implementation.recovery('sync'))

    def test_recorded_sync_without_intent_can_restart_before_submission(self):
        self.install(Lifecycle.SYNC,records_calls=True)
        self.change(status='submitting',attempts=1)
        self.recover()
        self.assertEqual(self.step()['status'],'pending')
        self.fixture.tick()
        self.assertEqual(self.step()['status'],'succeeded')
        self.assertEqual(self.fixture.adapter.calls.count(('local','test')),1)

    def test_sync_without_receipt_contract_pauses_unknown_after_crash(self):
        self.install(Lifecycle.SYNC)
        self.change(status='submitting',attempts=1)
        self.recover()
        self.assertEqual(self.step()['error_code'],'submission_unknown')
        self.assertEqual(self.fixture.adapter.calls,[])
        with self.assertRaises(ValueError):workflows.resume(self.store,self.fixture.turn)

    def test_sync_without_receipt_contract_does_not_retry_timeout_as_local_work(self):
        def lost_response(adapter,w,s,**kw):
            adapter.calls.append(('paid_request',s['id']))
            raise TimeoutError('response lost')
        self.install(Lifecycle.SYNC,handler=lost_response)
        self.fixture.tick();self.recover();self.fixture.tick()
        self.assertEqual(self.step()['error_code'],'submission_unknown')
        self.assertEqual(self.fixture.adapter.calls,[('paid_request','test')])

    def test_succeeded_receipt_without_persisted_output_never_reexecutes(self):
        self.install(Lifecycle.SYNC,records_calls=True)
        self.change(status='submitting',attempts=1)
        self.save_receipt(result=[])
        self.recover();self.fixture.tick()
        self.assertEqual(self.step()['error_code'],'result_unavailable')
        self.assertEqual(self.fixture.adapter.calls,[])

    def test_async_collect_recovery_queries_same_task_without_resubmitting(self):
        self.install(Lifecycle.ASYNC)
        self.fixture.tick()
        self.change(status='collecting')
        self.recover()
        self.assertEqual(self.step()['status'],'waiting')
        self.fixture.adapter.record={'status':'succeeded'}
        self.fixture.tick()
        self.assertEqual(self.step()['status'],'succeeded')
        self.assertEqual(self.fixture.adapter.calls.count(('submit','test')),1)
        self.assertIn(('poll','job-test'),self.fixture.adapter.calls)
        self.assertIn(('collect','test'),self.fixture.adapter.calls)

    def test_query_recovery_preserves_retry_deadline(self):
        self.install(Lifecycle.ASYNC)
        self.fixture.tick()
        deadline=self.store.now()+60
        self.change(next_poll=deadline,failures=2)
        with patch.object(Implementation,'recovery',autospec=True,side_effect=Implementation.recovery) as recovery:
            self.recover()
            self.assertTrue(any(call.args[1]=='poll' for call in recovery.call_args_list))
        self.assertEqual(self.step()['status'],'waiting')
        self.assertEqual(self.step()['next_poll'],deadline)
        self.assertEqual(self.step()['failures'],2)
        self.fixture.runner.tick(self.fixture.turn)
        self.assertEqual(self.fixture.adapter.calls,[('submit','test')])

    def test_local_recovery_enforces_retry_budget_then_runs_registered_handler(self):
        self.install(Lifecycle.LOCAL)
        self.change(status='running',attempts=1)
        self.recover();self.fixture.tick()
        self.assertEqual(self.step()['status'],'succeeded')
        self.assertEqual(self.fixture.adapter.calls.count(('local','test')),1)
        self.change(status='running',attempts=3)
        self.recover()
        self.assertEqual(self.step()['status'],'paused')
        self.assertFalse(workflows.public(self.fixture.get())['resumable'])
