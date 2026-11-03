"""Switches preserve business authority, budgets and external task identity."""
import json
import os
import unittest
from unittest.mock import patch
from webapp import agent_store, workflows
from webapp.agent_profiles import for_task
from webapp.harness_switching import request, reconcile, cancel, latest
from webapp.task_leases import Coordinator


class HarnessSwitchTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workflows import WorkflowTests
        self.fixture=WorkflowTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.s=self.fixture.store;self.project=self.fixture.project;self.task=self.fixture.turn
        self.settings=patch.dict(os.environ,{'VIDEO_USE_CLAUDE_API_KEY':'test-only-key',
            'VIDEO_USE_AGENT_PROFILES_JSON':json.dumps({'claude':{'harness':'claude_code','protocol':'anthropic',
                'model':'claude-test','base_url':'http://127.0.0.1:9876/v1','credential_env':'VIDEO_USE_CLAUDE_API_KEY','reasoning_effort':'high'}})})
        self.settings.start();self.addCleanup(self.settings.stop)
        self.available=patch('webapp.harness_adapters.ClaudeCodeAdapter.available',return_value=True)
        self.available.start();self.addCleanup(self.available.stop)
        self.source=for_task(self.s,self.task)

    def switch(self,key='request-1'):
        return request(self.s,self.project,self.task,'claude',key,self.source.digest)

    def test_waiting_external_job_is_not_resubmitted_when_harness_changes(self):
        self.fixture.submit();self.fixture.tick()
        before=workflows.get(self.s,self.task);step=before['steps'][0]
        self.assertEqual(step['external_id'],'job-generate')
        original_calls=list(self.fixture.adapter.calls)
        result=self.switch();reconcile(self.s,self.task)
        after=workflows.get(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'activated')
        self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['steps'][0]['external_id'],step['external_id'])
        self.assertEqual(after['steps'][0]['revision'],step['revision'])
        self.assertEqual(self.fixture.adapter.calls,original_calls)
        self.assertEqual(for_task(self.s,self.task).harness,'claude_code')
        self.assertEqual(for_task(self.s,self.task).budget,self.source.budget)
        self.assertEqual(self.switch()['id'],result['id'])

    def test_switch_waits_for_live_agent_and_fences_new_claims(self):
        self.s.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.task,))
        owner=Coordinator(self.s,'node-a','test');lease=owner.claim(self.task,'agent');self.assertIsNotNone(lease)
        self.switch();reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'waiting')
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)
        lease.release()
        next_owner=Coordinator(self.s,'node-b','test')
        self.assertIsNone(next_owner.claim(self.task,'agent'))
        self.s.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.task,))
        reconcile(self.s,self.task)
        acquired=next_owner.claim(self.task,'agent');self.assertIsNotNone(acquired);acquired.release()
        self.assertEqual(for_task(self.s,self.task).harness,'claude_code')

    def test_unknown_external_submission_blocks_handoff_without_replay(self):
        self.fixture.submit();w=workflows.get(self.s,self.task);step=w['steps'][0]
        from webapp.provider_calls import intent
        intent(self.s,w,step,'test-provider','image.edit')
        self.switch();reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'blocked')
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)
        self.assertEqual(self.fixture.adapter.calls,[])

    def test_expired_cleaned_attempt_does_not_wait_forever_or_accept_late_events(self):
        from webapp.harness_events import AgentEvent,record
        from webapp.task_leases import LeaseLost
        self.s.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(self.task,))
        owner=Coordinator(self.s,'old-node','test');lease=owner.claim(self.task,'agent')
        self.switch()
        self.s.execute('UPDATE execution_leases SET expires=? WHERE attempt=?',(self.s.now()-10,lease.attempt))
        self.s.execute("INSERT INTO execution_cleanup(attempt,task_id,node,state,updated) VALUES (?,?,?,'complete',?)",
                       (lease.attempt,self.task,owner.node,self.s.now()))
        reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'activated')
        with lease.bind(),self.assertRaises(LeaseLost):
            record(self.s,self.task,'codex',AgentEvent('message.completed','late',{'text':'stale'}),1,lambda text:text)
        self.assertFalse(self.s.query('SELECT seq FROM agent_context_events WHERE task_id=?',(self.task,)))

    def test_interrupted_running_task_requires_recovery_even_after_cleanup(self):
        self.s.execute("UPDATE agent_turns SET status='running' WHERE id=?",(self.task,))
        self.switch();reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'blocked')
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)

    def test_unclosed_tool_record_is_not_assumed_safe(self):
        self.fixture.submit()
        agent_store.event(self.s,self.task,'tool',{'id':'interrupted','status':'running','command':'external-effect','tool_type':'commandExecution'})
        self.switch();reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'blocked')
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)

    def test_cancel_wins_over_switch_and_does_not_reset_budget(self):
        self.fixture.submit();result=self.switch()
        self.s.execute("INSERT INTO execution_budgets VALUES (?,? ,?)",(self.task,'model',7))
        workflows.cancel(self.s,self.task);reconcile(self.s,self.task)
        self.assertEqual(latest(self.s,self.task)['state'],'cancelled')
        self.assertEqual(self.s.query('SELECT used FROM execution_budgets WHERE run_id=? AND category=?',(self.task,'model'))[0]['used'],7)
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)

    def test_withdraw_switch_does_not_change_task_or_workflow(self):
        self.fixture.submit();result=self.switch()
        cancelled=cancel(self.s,self.project,self.task,result['id'])
        self.assertEqual(cancelled['state'],'cancelled');reconcile(self.s,self.task)
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)
        self.assertEqual(workflows.get(self.s,self.task)['status'],'ready')

    def test_completed_task_keeps_historical_profile_and_changes_future_preference(self):
        agent_store.finish(self.s,self.task,'succeeded','completed')
        self.assertEqual(self.switch()['state'],'next_turn')
        self.assertEqual(for_task(self.s,self.task).digest,self.source.digest)
        task=agent_store.add_turn(self.s,self.project,'plan','next request')
        self.assertEqual(for_task(self.s,task).harness,'claude_code')

    def test_stale_configuration_and_foreign_project_are_rejected(self):
        with self.assertRaises(ValueError):request(self.s,'foreign',self.task,'claude','r',self.source.digest)
        with self.assertRaises(ValueError):request(self.s,self.project,self.task,'claude','r','0'*64)
        self.assertFalse(self.s.query('SELECT id FROM agent_harness_switches'))

    def test_activation_is_persistent_and_not_reapplied_after_restart(self):
        self.fixture.submit();self.switch();reconcile(self.s,self.task)
        generation=self.s.query('SELECT generation FROM agent_run_profiles WHERE task_id=?',(self.task,))[0]['generation']
        from webapp.store import Store
        other=Store(self.s.root);reconcile(other,self.task)
        self.assertEqual(other.query('SELECT generation FROM agent_run_profiles WHERE task_id=?',(self.task,))[0]['generation'],generation)
        handoff=json.loads(other.query('SELECT handoff FROM agent_harness_switches WHERE task_id=?',(self.task,))[0]['handoff'])
        self.assertEqual(handoff['task']['id'],self.task)
        self.assertEqual(handoff['workflow']['id'],workflows.get(self.s,self.task)['id'])


class HarnessAPITests(unittest.TestCase):
    def test_configuration_and_switch_endpoints_enforce_project_ownership(self):
        from tests.test_agent_isolation import AgentAccessTests
        fixture=AgentAccessTests();fixture.setUp()
        try:
            endpoint=f'/api/projects/{fixture.pid}/agent'
            self.assertEqual(fixture.b.get(endpoint+'/profiles').status_code,404)
            self.assertEqual(fixture.b.put(endpoint+'/profile',json={'profile':'default'}).status_code,404)
            task=fixture.plan();data=fixture.a.get(endpoint).json()['turns'][-1]
            response=fixture.b.post(endpoint+f'/{task}/harness-switch',json={'profile':'default','request_key':'test','expected_digest':data['agent_profile']['digest']})
            self.assertEqual(response.status_code,404)
            own=fixture.a.get(endpoint+'/profiles').json()
            self.assertTrue(own['profiles']);self.assertNotIn('credential_env',json.dumps(own));self.assertNotIn('base_url',json.dumps(own))
        finally:fixture.tearDown()
