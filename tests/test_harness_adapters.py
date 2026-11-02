"""Public protocol and configuration contracts, independent of private sessions."""
import json
import os
import unittest
from unittest.mock import patch
from webapp.agent_profiles import AgentProfile, selected, for_task
from webapp.harness_adapters import CodexAdapter, get
from webapp.harness_sdk_events import SDKEvents
from webapp.model_adapters import AnthropicAdapter, ChatCompletionsAdapter, AnthropicReceipt, ChatReceipt


class HarnessProtocolTests(unittest.TestCase):
    def test_unknown_harness_and_invalid_protocol_are_rejected(self):
        with self.assertRaises(ValueError):get('unregistered')
        with self.assertRaises(ValueError):AgentProfile(harness='claude_code',protocol='responses')
        with self.assertRaises(ValueError):AgentProfile(credential_env='HOME')
        with self.assertRaises(ValueError):AgentProfile(reasoning_effort='guess')

    def test_profile_contains_only_credential_reference_and_public_projection_omits_route(self):
        profile=AgentProfile(base_url='https://example.org/v1')
        with patch.dict(os.environ,{'VIDEO_USE_MODEL_API_KEY':'private-test-secret'}):
            self.assertNotIn('private-test-secret',json.dumps(profile.snapshot()))
            self.assertNotIn('base_url',profile.public())
            self.assertNotIn('credential_env',profile.public())
            self.assertEqual(profile.credentials()[1],'private-test-secret')

    def test_codex_session_and_turn_honor_same_effective_configuration(self):
        calls=[]
        def rpc(method,params):
            calls.append((method,params));return {'thread':{'id':'native-thread'}}
        adapter=CodexAdapter();profile=AgentProfile(reasoning_effort='high')
        session=adapter.open_session(rpc,profile,'trusted rules')
        adapter.run_turn(rpc,session,'request',profile)
        self.assertEqual(calls[0][0],'thread/start')
        self.assertEqual(calls[1][1]['effort'],'high')
        adapter.open_session(rpc,profile,'trusted rules',session)
        self.assertEqual(calls[-1][0],'thread/resume')

    def test_codex_public_events_ignore_reasoning_and_preserve_complete_text(self):
        adapter=CodexAdapter()
        self.assertEqual(adapter.events({'method':'item/reasoning/textDelta','params':{'delta':'private'}}),[])
        delta=adapter.events({'method':'item/agentMessage/delta','params':{'itemId':'m','delta':'prefix'}})[0]
        final=adapter.events({'method':'item/completed','params':{'item':{'type':'agentMessage','id':'m','text':'prefix tail'}}})[0]
        self.assertEqual((delta.kind,delta.id),('message.delta','m'))
        self.assertEqual((final.kind,final.data['text']),('message.completed','prefix tail'))

    def test_claude_public_stream_and_actual_tool_hooks_keep_correlations(self):
        rows=[];codec=SDKEvents(lambda *x:rows.append(x))
        codec.claude_stream({'type':'message_start','message':{'id':'message-1'}})
        codec.claude_stream({'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private'}})
        codec.claude_stream({'type':'content_block_delta','delta':{'type':'text_delta','text':'hello'}})
        codec.claude_message([{'type':'text','text':'hello world'}])
        for name in ('PreToolUse','PostToolUseFailure'):
            codec.claude_hook({'hook_event_name':name,'tool_name':'Bash','tool_use_id':'tool-1','tool_input':{'command':'false'},'error':'exit 1'})
        self.assertEqual([x[0] for x in rows],['message.delta','message.completed','tool.started','tool.completed'])
        self.assertEqual(rows[0][1],rows[1][1])
        self.assertEqual(rows[-1][2]['item']['status'],'failed')
        self.assertNotIn('private',json.dumps(rows))

    def test_native_image_urls_and_provider_tools_cannot_bypass_gateway(self):
        body={'messages':[{'role':'user','content':[{'type':'image','source':{'type':'url','url':'http://127.0.0.1/private'}}]}]}
        with self.assertRaises(ValueError):AnthropicAdapter().prepare(body,'m',100)
        for adapter,tools in [(AnthropicAdapter(),[{'type':'web_search_20250305','name':'web_search'}]),
                              (ChatCompletionsAdapter(),[{'type':'web_search'}])]:
            with self.subTest(protocol=adapter.protocol),self.assertRaises(ValueError):
                adapter.prepare({'messages':[{'role':'user','content':'test'}],'tools':tools},'m',100)
        for malformed in (['not a tool'], [None], [42]):
            with self.assertRaises(ValueError):ChatCompletionsAdapter().prepare({'messages':[],'tools':malformed},'m',100)
        with self.assertRaises(ValueError):
            AnthropicAdapter().prepare({'messages':[{'content':[{'type':'image','source':'malformed'}]}]},'m',100)

    def test_anthropic_receipts_are_cumulative_and_cache_input_is_counted_once(self):
        messages=[{'type':'message_start','message':{'id':'msg_1','usage':{'input_tokens':12,'cache_read_input_tokens':3,'cache_creation_input_tokens':2,'output_tokens':1}}},
                  {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':8}},
                  {'type':'message_stop'}]
        wire=b''.join(('event: '+m['type']+'\r\ndata: '+json.dumps(m)+'\r\n\r\n').encode() for m in messages)
        for width in (1,7,len(wire)):
            receipt=AnthropicReceipt()
            for start in range(0,len(wire),width):receipt.feed(wire[start:start+width])
            receipt.finish()
            self.assertEqual(receipt.usage,{'input_tokens':17,'output_tokens':8,'total_tokens':None,'cached_tokens':3,'reasoning_tokens':None})
            self.assertEqual(receipt.status,'succeeded')

    def test_partial_anthropic_stream_does_not_claim_final_output_usage(self):
        receipt=AnthropicReceipt();receipt.accept(json.dumps({'type':'message_start','message':{'id':'msg','usage':{'input_tokens':12,'output_tokens':1}}}))
        receipt.finish();self.assertEqual(receipt.usage['input_tokens'],12)
        self.assertIsNone(receipt.usage['output_tokens']);self.assertIsNone(receipt.usage['total_tokens']);self.assertIsNone(receipt.status)

    def test_effective_reasoning_policy_cannot_be_overridden_by_native_request(self):
        from webapp.model_adapters import ResponsesAdapter
        body=ResponsesAdapter().prepare({'input':[],'reasoning':{'effort':'low'}},'locked-model',100,'high')
        self.assertEqual(body['reasoning']['effort'],'high');self.assertEqual(body['model'],'locked-model')
        body=AnthropicAdapter().prepare({'messages':[],'output_config':{'effort':'low'}},'locked-model',100,'high')
        self.assertEqual(body['output_config']['effort'],'high')

    def test_chat_missing_usage_is_unknown_and_truncation_is_not_success(self):
        receipt=ChatReceipt();receipt.accept(json.dumps({'id':'chat_1','choices':[{'finish_reason':'length'}]}))
        self.assertIsNone(receipt.usage);self.assertEqual(receipt.status,'incomplete')
        receipt.accept(json.dumps({'id':'chat_1','choices':[],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,'prompt_cache_hit_tokens':4}}))
        self.assertEqual(receipt.usage['cached_tokens'],4)
        self.assertIsNone(receipt.usage['reasoning_tokens'])


class FrozenAgentProfileTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workflows import WorkflowTests
        self.fixture=WorkflowTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store

    def test_task_budget_and_route_do_not_follow_later_defaults(self):
        task=self.fixture.turn
        before=for_task(self.store,task)
        with patch.dict(os.environ,{'VIDEO_USE_MODEL':'another-model','VIDEO_USE_TASK_MODEL_CALLS':'900','VIDEO_USE_PLAN_MODELS':'100'}):
            self.assertNotEqual(selected().digest,before.digest)
            self.assertEqual(for_task(self.store,task).snapshot(),before.snapshot())

    def test_non_light_harnesses_reserve_full_resources_before_claim(self):
        from webapp.harness_adapters import can_use_light
        from webapp.plan_readiness import register
        from webapp.scheduling import requirements
        from webapp.task_leases import Coordinator
        from webapp import agent_store
        profiles={name:{'harness':name,'protocol':'anthropic','model':'test','reasoning_effort':'high'}
                  for name in ('claude_code','deepseek_harness')}
        with patch.dict(os.environ,{'VIDEO_USE_AGENT_PROFILES_JSON':json.dumps(profiles),'VIDEO_USE_NODE_MEMORY_MB':'1024'}):
            coordinator=Coordinator(self.store,'resource-regression-node','test')
            for harness in ('claude_code','deepseek_harness','default'):
                with patch.dict(os.environ,{'VIDEO_USE_AGENT_PROFILE':harness}):
                    project=self.store.add_project('resource-owner-'+harness,'Resource admission')['id']
                    task=agent_store.add_turn(self.store,project,'plan','Hello')
                register(self.store,task,'light')
                light=harness=='default'
                self.assertEqual(can_use_light(self.store,task),light)
                self.assertEqual(requirements(self.store,task,'agent')['memory_mb'],256 if light else 4096)
                lease=coordinator.claim(task,'agent')
                if light:
                    self.assertIsNotNone(lease);lease.release()
                else:
                    self.assertIsNone(lease,'A 1GB node must not admit a full 4GB Harness as lightweight chat')
                    self.assertFalse(self.store.query('SELECT attempt FROM execution_resource_claims WHERE task_id=?',(task,)))

    def test_portable_handoff_contains_authoritative_state_and_explicit_coverage(self):
        from webapp.harness_context import build
        task=self.fixture.turn;row=self.store.query('SELECT project,prompt FROM agent_turns WHERE id=?',(task,))[0]
        value=build(self.store,row['project'],task)
        self.assertEqual(value['task']['prompt'],row['prompt'])
        self.assertEqual(value['coverage']['native_internal_state'],'not_transferred')
        self.assertIn('conversation',value['indexes'])
        with self.assertRaises(ValueError):build(self.store,'another-project',task)

    def test_history_paging_recovers_old_constraints_and_redacts_before_slicing(self):
        from webapp.harness_context import lookup
        from webapp import agent_store
        project=self.fixture.project;original=self.fixture.turn
        agent_store.finish(self.store,original,'succeeded','done')
        secret='private-paging-test-key'
        with patch.dict(os.environ,{'VIDEO_USE_MODEL_API_KEY':secret}):
            for i in range(24):
                task=agent_store.add_turn(self.store,project,'plan',('x'*1995+secret+' original constraint') if i==0 else f'question {i}')
                agent_store.finish(self.store,task,'succeeded','A'*15995+secret+' tail')
                if i==0:old=task
            first=lookup(self.store,project,{'action':'turns'})
            second=lookup(self.store,project,{'action':'turns','before':first['next_before']})
            self.assertIn(old,[row['id'] for row in second['turns']])
            self.assertNotIn(secret[:5],json.dumps(second))
            result='';offset=0
            while True:
                part=lookup(self.store,project,{'action':'message','task':old,'field':'text','offset':offset})
                result+=part['text'];offset=part['next_offset']
                if offset is None:break
            self.assertTrue(result.endswith(' tail'));self.assertNotIn(secret,result)
            for body in ({'action':'message','task':old,'field':'configuration'},
                         {'action':'message','task':old,'offset':-1},
                         {'action':'turns','project':'foreign'}):
                with self.assertRaises(ValueError):lookup(self.store,project,body)
            with self.assertRaises(ValueError):lookup(self.store,'foreign',{'action':'message','task':old})

    def test_checkpoint_envelope_ignores_uncommitted_session_and_foreign_configuration(self):
        from webapp.checkpoints import profile,save_native_descriptor,committed_session,native_descriptor
        from webapp.harness_switching import record_session
        from webapp.task_leases import Coordinator
        from webapp import agent_store
        task=self.fixture.turn;project=self.fixture.project
        self.store.execute("UPDATE agent_turns SET status='queued' WHERE id=?",(task,))
        lease=Coordinator(self.store,'envelope-node','test').claim(task,'agent')
        try:
            with lease.bind():
                identity=for_task(self.store,task)
                record_session(self.store,project,identity,'committed-native','public_context')
                self.store.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?)',(project,lease.attempt,'checkpoint-1','{"workspace":{},"state":{}}',self.store.now()))
                with self.store.connect() as db:
                    save_native_descriptor(self.store,db,project,'checkpoint-1',{'state':{}},profile())
                record_session(self.store,project,identity,'uncommitted-after-crash','public_context')
                self.assertEqual(committed_session(self.store,project),'committed-native')
                self.assertEqual(native_descriptor(self.store,project,'checkpoint-1')['native_format'],'sdk_owned_opaque')
                self.assertIsNone(native_descriptor(self.store,'foreign','checkpoint-1'))
                from dataclasses import replace
                lease.agent_profile=replace(identity,model='incompatible-model')
                self.assertIsNone(committed_session(self.store,project))
        finally:lease.release()
