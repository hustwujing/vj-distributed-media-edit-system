import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from webapp.agent_profiles import AgentProfile
from webapp.sandbox_backends import select_backend,BackendConfigurationError


class NativeSelectionTests(unittest.TestCase):
    def test_user_harness_selects_exact_immutable_runtime_and_missing_never_falls_back(self):
        mapping={name:{'runtime_profile':name+'-v1','image_digest':name+'@sha256:'+char*64,
                       'skill_digest':'sha256:'+'d'*64}
                 for name,char in [('codex','a'),('claude_code','b'),('deepseek_harness','c')]}
        env={'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox',
             'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':json.dumps(mapping)}
        for harness,expected in mapping.items():
            with self.subTest(harness=harness):
                binding=select_backend({'gpu':False,'sandbox_kind':'agent_runtime','harness':harness},environ=env)
                self.assertEqual(binding.image_digest,expected['image_digest'])
                self.assertEqual(binding.runtime_kind,'agent_runtime')
        with self.assertRaises(BackendConfigurationError):
            select_backend({'gpu':False,'harness':'unknown'},environ=env)
        mapping['codex']['image_digest']='agent:latest'
        env['VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON']=json.dumps(mapping)
        with self.assertRaises(BackendConfigurationError):select_backend({'gpu':False,'harness':'codex'},environ=env)
        self.assertEqual(select_backend({'gpu':True,'harness':'codex'},environ=env).backend,'local_bwrap')

    def test_only_code_editor_switches_strategy_and_runtime_changes_invalidate_its_snapshot(self):
        from webapp.specialist_profiles import get,catalogue
        with patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'local_bwrap'}):
            old=get('editing_implementer').snapshot()
        with patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox',
                                   'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':'{}'}):
            current=get('editing_implementer').snapshot()
            self.assertEqual(current['harness'],'native-implementer-v1')
            self.assertNotEqual(old,current)
            for item in catalogue():
                if item['id']!='editing_implementer':self.assertEqual(item['harness'],'structured-profile-v4')
            with patch.dict(os.environ,{'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':'{"version":2}'}):
                self.assertNotEqual(get('editing_implementer').snapshot()['harness_digest'],current['harness_digest'])

    def test_attempt_selection_uses_frozen_role_not_current_default(self):
        from webapp.agent_host import AgentHost
        host=AgentHost.__new__(AgentHost);host.store=Mock();host.runtime_backends=Mock()
        lease=SimpleNamespace(run_id='run',kind='workflow',step_id='edit')
        work={'dynamic':True,'steps':[{'id':'edit','spec':{'kind':'agent'}}]}
        task={'id':'edit','type':'agent','spec':{'profile':'editing_implementer'},
              'profile':{'harness':'native-implementer-v1'}}
        with patch('webapp.scheduling.requirements',return_value={'gpu':False}), \
             patch('webapp.workflows.get',return_value=work), \
             patch('webapp.workflow_graph.get',return_value={'tasks':[task]}), \
             patch('webapp.workflow_graph.work_item',return_value=task), \
             patch('webapp.agent_profiles.for_specialist',return_value=SimpleNamespace(harness='deepseek_harness')), \
             patch('webapp.agent_host.select_backend') as choose:
            host._runtime(lease)
            self.assertEqual(choose.call_args.args[0]['harness'],'deepseek_harness')
            self.assertEqual(choose.call_args.args[0]['sandbox_kind'],'agent_runtime')

    def test_actual_image_blocks_exclude_tool_arguments_and_schema_examples(self):
        from webapp.model_input_images import images
        image={'type':'input_image','image_url':'data:image/jpeg;base64,YQ=='}
        body={'input':[{'role':'user','content':[image]},
                       {'type':'function_call','arguments':{'content':[image]}}],
              'tools':[{'parameters':{'example':image}}]}
        self.assertEqual(images(body),[image['image_url']])


class NativeRunnerTests(unittest.TestCase):
    def run_case(self,status='completed',stop=False):
        from webapp.native_implementer import execute
        from webapp.specialist_profiles import get
        from webapp.harness_events import AgentEvent
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox'}))
        profile=get('editing_implementer')
        task={'id':'edit','revision':2,'profile':profile.snapshot(),'repair_context':{},
              'spec':{'profile':'editing_implementer','timeout_seconds':60,'output_roles':{'report':'result.txt'}}}
        run={'id':'run','project_id':'project'}
        runtime=Mock(is_native=True);runtime.gateway.last_error=None
        configuration=AgentProfile(harness='codex',model='gpt-6-astra')
        host=SimpleNamespace(store=Mock(),stop=threading.Event(),redact=lambda x:x,notify=Mock())
        if stop:host.stop.set()
        lease=Mock(runtime=runtime,run_id='run',attempt='attempt',generation=4,step_id='edit')
        lease.lost=threading.Event()
        callback=[];runtime.start_harness.side_effect=lambda c,b,e,**kwargs:callback.append(e)
        runtime.client.call.side_effect=lambda method,args: {'session_id':'native-session'} if method=='session.open' else {}
        def receive():
            callback[0](AgentEvent('message.delta','m',{'delta':'处理输出'}))
            callback[0](AgentEvent('message.completed','m',{'text':'结果已生成','phase':'final_answer'}))
            callback[0](AgentEvent('turn.ended','t',{'status':status,'error':'fixture failure' if status!='completed' else None}))
        runtime.client.receive_once.side_effect=receive
        stack.enter_context(patch('webapp.agent_profiles.for_specialist',return_value=configuration))
        stack.enter_context(patch('webapp.agent_profiles.record_attempt'))
        stack.enter_context(patch('webapp.native_sessions.identity',return_value='config'))
        stack.enter_context(patch('webapp.native_sessions.load',return_value=None))
        stack.enter_context(patch('webapp.native_sessions.save'))
        stack.enter_context(patch('webapp.workflow_agents.output_directory',return_value=('edit/workflows/run/edit/out',{'assets':[]})))
        stack.enter_context(patch('webapp.workflow_graph.execution_view',return_value={'steps':[{'id':'edit'}]}))
        stack.enter_context(patch('webapp.workflow_inputs.resolve',return_value={'source':{'path':'sources/a.txt','sha256':'a'*64}}))
        publisher=stack.enter_context(patch('webapp.agent_text.TextPublisher')).return_value
        stack.enter_context(patch('webapp.harness_events.record'))
        finish=stack.enter_context(patch('webapp.workflow_agents.finish_files',return_value='candidate-staged'))
        return lambda:execute(host,lease,run,task),runtime,publisher,finish

    def test_native_completion_only_stages_through_existing_independent_checks(self):
        run,runtime,publisher,finish=self.run_case()
        self.assertEqual(run(),'candidate-staged')
        runtime.freeze_harness.assert_called_once();runtime.begin_cleanup.assert_called_once()
        runtime._download_output.assert_called_once()
        finish.assert_called_once();self.assertEqual(finish.call_args.args[-2:],({},'edit/workflows/run/edit/out'))
        publisher.message.assert_called();publisher.flush.assert_called_with(complete=True)
        self.assertTrue(publisher.message.call_args.args[1].startswith('代码编辑师：'))
        prompt=next(args[1]['text'] for args,kwargs in runtime.client.call.call_args_list if args[0]=='turn.start')
        self.assertEqual(json.loads(prompt)['inputs']['source']['path'],'/workspace/sources/a.txt')

    def test_harness_failure_does_not_collect_or_stage_success_and_flushes_public_text(self):
        run,runtime,publisher,finish=self.run_case(status='failed')
        with self.assertRaisesRegex(RuntimeError,'fixture failure'):run()
        finish.assert_not_called();runtime._download_output.assert_not_called()
        publisher.flush.assert_called_with(complete=True)

    def test_executor_stop_propagates_fencing_without_staging(self):
        from webapp.task_leases import LeaseLost
        run,runtime,publisher,finish=self.run_case(stop=True)
        with self.assertRaises(LeaseLost):run()
        finish.assert_not_called();publisher.flush.assert_called_with(complete=True)


if __name__=='__main__':unittest.main()
