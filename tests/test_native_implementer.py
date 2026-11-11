import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from webapp.agent_profiles import AgentProfile
from webapp.sandbox_backends import select_backend,BackendConfigurationError


class NativeSelectionTests(unittest.TestCase):
    @staticmethod
    def agent_spec(**runtime):
        return {'id':'edit','type':'agent','name':'Edit','objective':'Generate authorized output',
            'profile':'editing_implementer','output_roles':{'report':'result.txt'},
            'acceptance':[{'kind':'file'}],'basic_reason':'Delegated local implementation',
            'timeout_seconds':1800 if runtime.get('native_skills') else 900,
            'runtime_requirements':runtime}

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
        from webapp.specialist_profiles import get,for_task,authorization_snapshot,catalogue
        spec={'profile':'editing_implementer','runtime_requirements':{'requires_gpu':False}}
        with patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'local_bwrap'}):
            old=for_task(spec).snapshot()
        with patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox',
                                   'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':'{"codex":{}}'}):
            current=for_task(spec).snapshot()
            self.assertEqual(current['harness'],'native-implementer-v1')
            self.assertNotEqual(old,current)
            self.assertEqual(for_task({'profile':'editing_implementer','runtime_requirements':{'requires_gpu':True}}).harness,'structured-profile-v4')
            self.assertEqual(get('editing_implementer').harness,'structured-profile-v4')
            for item in catalogue():
                if item['id']=='editing_implementer':
                    self.assertFalse(item['runtime_options']['opensandbox_cpu']['requires_gpu'])
                    self.assertTrue(item['runtime_options']['bwrap_gpu']['requires_gpu'])
                else:self.assertEqual(item['harness'],'structured-profile-v4')
            authorized=authorization_snapshot('editing_implementer')
            with patch.dict(os.environ,{'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':'{"version":2}'}):
                self.assertNotEqual(authorization_snapshot('editing_implementer'),authorized)

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

    def test_declared_gpu_requirement_routes_only_gpu_agent_to_bwrap(self):
        from webapp import workflow_graph as graph
        from webapp.specialist_profiles import for_task
        from webapp.workflow_dispatch import requirements
        environment={'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox'}
        with patch.dict(os.environ,environment):
            cpu=graph.Task.model_validate(self.agent_spec(requires_gpu=False,
                python_modules=['numpy'],system_tools=['ffmpeg'],network_urls=[]))
            gpu=graph.Task.model_validate(self.agent_spec(requires_gpu=True,
                python_modules=['numpy'],system_tools=['ffmpeg'],network_urls=[]))
            self.assertEqual(for_task(cpu).harness,'native-implementer-v1')
            self.assertEqual(for_task(gpu).harness,'structured-profile-v4')
            for spec,expected in ((cpu,False),(gpu,True)):
                policy=for_task(spec)
                run={'tasks':[{'id':'edit','type':'agent','spec':spec.model_dump(),
                               'profile':policy.snapshot(),'selected':1}],'control_jobs':[]}
                resource=requirements(Mock(),run,'edit')
                self.assertEqual(resource['gpu'],expected)
                self.assertEqual(resource['runtime_target'],'bwrap_gpu' if expected else 'opensandbox_cpu')

    def test_native_skill_selection_is_manifest_bound_and_opensandbox_only(self):
        from webapp import workflow_graph as graph
        from webapp.agent_runtime_capabilities import catalogue,validate
        available=catalogue()['opensandbox_cpu']['native_skills']
        self.assertEqual(len(available),25);self.assertIn('academy-guide',available)
        task=graph.Task.model_validate(self.agent_spec(requires_gpu=False,native_skills=['academy-guide']))
        self.assertEqual(task.runtime_requirements.native_skills,['academy-guide'])
        with self.assertRaisesRegex(ValueError,'profile maximum'):
            graph.Task.model_validate({**self.agent_spec(requires_gpu=False,native_skills=['academy-guide']),
                                       'timeout_seconds':900})
        self.assertEqual(validate(task.runtime_requirements.model_dump(),selected='opensandbox_cpu'),'opensandbox_cpu')
        for runtime in ({'requires_gpu':True,'native_skills':['academy-guide']},
                        {'requires_gpu':False,'native_skills':['missing-skill']}):
            with self.subTest(runtime=runtime),self.assertRaises(ValueError):
                graph.Task.model_validate(self.agent_spec(**runtime))
        with self.assertRaises(ValueError):validate({'native_skills':['academy-guide']},selected='bwrap_cpu')

    def test_model_harness_without_native_image_freezes_structured_cpu_fallback(self):
        from webapp.specialist_profiles import for_task
        from webapp.workflow_dispatch import requirements
        spec=self.agent_spec(requires_gpu=False,python_modules=['numpy'],system_tools=['python'],network_urls=[])
        env={'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox',
             'VIDEO_USE_OPEN_SANDBOX_AGENT_PROFILES_JSON':'{"claude_code":{}}'}
        with patch.dict(os.environ,env,clear=False):
            native=for_task(spec,model_harness='claude_code')
            fallback=for_task(spec,model_harness='deepseek_harness')
            self.assertEqual(native.harness,'native-implementer-v1')
            self.assertEqual(fallback.harness,'structured-profile-v4')
            run={'tasks':[{'id':'edit','type':'agent','spec':spec,
                           'profile':fallback.snapshot(),'selected':1}],'control_jobs':[]}
            resource=requirements(Mock(),run,'edit')
            self.assertFalse(resource['gpu']);self.assertEqual(resource['runtime_target'],'bwrap_cpu')

    def test_missing_cpu_dependency_is_rejected_instead_of_falling_back_to_bwrap(self):
        from webapp.agent_runtime_capabilities import validate
        with patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox'}):
            with self.assertRaisesRegex(ValueError,'Python modules: torch'):
                validate({'requires_gpu':False,'python_modules':['torch'],'system_tools':[],'network_urls':[]})

    def test_media_document_and_browser_dependencies_are_advertised_consistently(self):
        from webapp.agent_runtime_capabilities import catalogue,validate
        python={'av','cairosvg','ffmpeg','imageio','onnxruntime','pymupdf','rembg','reportlab','shapely','skimage','trimesh'}
        base_node={'esbuild','playwright','three'}
        skill_node=base_node|{'docx','pptxgenjs','react','react-dom','react-icons','sharp','typescript'}
        base_tools={'chromium','esbuild','node'}
        skill_tools=base_tools|{'dot','drawio','libreoffice','pandoc','pdftoppm','pdftotext',
                                'pnpm','qpdf','tesseract','tsc','zip'}
        for name,capabilities in catalogue().items():
            with self.subTest(runtime=name):
                self.assertTrue(python<=set(capabilities['python_modules']))
                self.assertEqual(set(capabilities['node_modules']),
                    skill_node if name=='opensandbox_cpu' else base_node)
                self.assertTrue((skill_tools if name=='opensandbox_cpu' else base_tools)
                    <=set(capabilities['system_tools']))
        self.assertEqual(validate({'requires_gpu':False,'python_modules':['rembg'],
            'node_modules':['playwright','three'],'system_tools':['chromium'],'network_urls':[]},
            selected='opensandbox_cpu'),'opensandbox_cpu')
        with self.assertRaisesRegex(ValueError,'Node modules: unregistered'):
            validate({'requires_gpu':False,'python_modules':[],'node_modules':['unregistered'],
                'system_tools':[],'network_urls':[]},selected='opensandbox_cpu')

    def test_operation_catalogue_exposes_authoritative_gpu_requirement(self):
        from webapp.operations import catalog
        operations=catalog()['operations']
        self.assertTrue(all(item['execution_requirements']['requires_gpu']==item['gpu'] for item in operations))
        self.assertTrue(any(item['execution_requirements']['requires_gpu'] for item in operations))

    def test_native_runtime_honors_model_specific_code_editor_output_limit(self):
        from dataclasses import replace
        from webapp.native_implementer_runtime import output_token_limit
        profile=AgentProfile(budget={
            'plan_seconds':240,'plan_models':14,'plan_tools':24,'plan_inspections':8,
            'run_seconds':900,'run_models':160,'task_models':40,'max_output_tokens':16000})
        self.assertEqual(output_token_limit(profile),12000)
        limited=replace(profile,specialist_output_tokens={'editing_implementer':4096})
        self.assertEqual(output_token_limit(limited),4096)

    def test_native_instructions_apply_the_shared_path_contract(self):
        from webapp.native_implementer import instructions
        from webapp.specialist_profiles import get
        text=instructions(get('editing_implementer'),[{'id':'academy-guide','path':'/state/codex/skills/academy-guide/SKILL.md','entry_sha256':'a'*64}],'codex')
        self.assertIn('/workspace 是稳定Shell工作目录',text)
        self.assertIn('临时工作文件只能放在VIDEO_USE_STEP_SCRATCH',text)
        self.assertIn('不得删除、移动或改名VIDEO_USE_STEP_OUTPUT本身',text)
        self.assertIn('完成全部声明文件和必要验收后立即结束并提交',text)
        self.assertIn('不得为可选解释重复读取未变化的输入或成果',text)
        self.assertIn('/state/codex/skills/academy-guide/SKILL.md',text)
        self.assertIn('不得用通用实现冒充',text)


class NativeRunnerTests(unittest.TestCase):
    def run_case(self,status='completed',stop=False,error=None):
        from webapp.native_implementer import execute
        from webapp.specialist_profiles import for_task
        from webapp.harness_events import AgentEvent
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ,{'VIDEO_USE_CPU_AGENT_SANDBOX_BACKEND':'opensandbox'}))
        spec={'profile':'editing_implementer','timeout_seconds':60,'output_roles':{'report':'result.txt'},
              'runtime_requirements':{'requires_gpu':False,'python_modules':[],'system_tools':[],'network_urls':[],
                                      'native_skills':['academy-guide']}}
        profile=for_task(spec)
        task={'id':'edit','revision':2,'profile':profile.snapshot(),'repair_context':{},
              'spec':spec}
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
            callback[0](AgentEvent('turn.ended','t',{'status':status,
                'error':error if error is not None else 'fixture failure' if status!='completed' else None}))
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
        context=runtime.start_harness.call_args.kwargs['task_context']
        self.assertEqual(context['native_skills'][0]['id'],'academy-guide')
        self.assertEqual(context['native_skills'][0]['path'],'/state/codex/skills/academy-guide/SKILL.md')
        finish.assert_called_once();self.assertEqual(finish.call_args.args[-2:],({},'edit/workflows/run/edit/out'))
        publisher.message.assert_called();publisher.flush.assert_called_with(complete=True)
        self.assertTrue(publisher.message.call_args.args[1].startswith('代码编辑师：'))
        prompt=next(args[1]['text'] for args,kwargs in runtime.client.call.call_args_list if args[0]=='turn.start')
        context=json.loads(prompt)
        self.assertEqual(context['inputs']['source']['path'],'/workspace/sources/a.txt')
        self.assertEqual(context['scratch_directory'],context['output_directory']+'/.scratch')
        self.assertEqual(context['path_policy']['rules'],[
            'inputs_are_read_only','writes_stay_under_output_root','temporary_files_stay_under_scratch_root',
            'shell_cwd_is_stable','cleanup_is_scratch_descendants_only','output_root_is_never_deleted',
            'scratch_is_not_checkpointed'])

    def test_harness_failure_does_not_collect_or_stage_success_and_flushes_public_text(self):
        run,runtime,publisher,finish=self.run_case(status='failed')
        with self.assertRaisesRegex(RuntimeError,'fixture failure'):run()
        finish.assert_not_called();runtime._download_output.assert_not_called()
        publisher.flush.assert_called_with(complete=True)

    def test_incomplete_model_stream_is_runtime_failure_not_script_repair(self):
        from webapp.runtime_client import RuntimeDependencyError
        run,runtime,publisher,finish=self.run_case(status='failed',
            error={'code':'STREAM_CLOSED','message':'stream ended before message_stop'})
        with self.assertRaisesRegex(RuntimeDependencyError,'协议兼容性'):run()
        finish.assert_not_called();runtime._download_output.assert_not_called()
        publisher.flush.assert_called_with(complete=True)

    def test_codex_concurrency_limit_is_a_transient_model_error_not_script_failure(self):
        from webapp.workflow_agents import ModelServiceError
        run,runtime,publisher,finish=self.run_case(status='failed',error={
            'message':'stream disconnected before completion: Concurrency limit exceeded for account, please retry later',
            'codexErrorInfo':'other'})
        with self.assertRaises(ModelServiceError) as raised:run()
        self.assertEqual(raised.exception.diagnostic['http_status'],429)
        self.assertEqual(raised.exception.diagnostic['code'],'model_provider_concurrency')
        finish.assert_not_called();runtime._download_output.assert_not_called()
        publisher.flush.assert_called_with(complete=True)

    def test_codex_stream_disconnect_is_a_transient_model_service_error(self):
        from webapp.workflow_agents import ModelServiceError
        run,runtime,publisher,finish=self.run_case(status='failed',error={
            'message':'stream disconnected before completion: upstream closed'})
        with self.assertRaises(ModelServiceError) as raised:run()
        self.assertEqual(raised.exception.diagnostic['http_status'],503)
        self.assertEqual(raised.exception.diagnostic['code'],'model_stream_disconnected')
        finish.assert_not_called();runtime._download_output.assert_not_called()
        publisher.flush.assert_called_with(complete=True)

    def test_gateway_business_rejection_is_reported_as_host_error(self):
        run,runtime,publisher,finish=self.run_case(status='failed',error='SDK received HTTP 422')
        runtime.gateway.last_error=ValueError('Messages request field is unsupported')
        with self.assertRaisesRegex(ValueError,'request field is unsupported'):run()
        finish.assert_not_called();runtime._download_output.assert_not_called()

    def test_executor_stop_propagates_fencing_without_staging(self):
        from webapp.task_leases import LeaseLost
        run,runtime,publisher,finish=self.run_case(stop=True)
        with self.assertRaises(LeaseLost):run()
        finish.assert_not_called();publisher.flush.assert_called_with(complete=True)

    def test_control_stream_reconnect_does_not_restart_native_turn(self):
        from webapp.runtime_client import RuntimeTransportError
        run,runtime,publisher,finish=self.run_case()
        successful=runtime.client.receive_once.side_effect;calls=[]
        def receive():
            calls.append(True)
            if len(calls)==1:raise RuntimeTransportError('injected disconnect')
            successful()
        runtime.client.receive_once.side_effect=receive
        self.assertEqual(run(),'candidate-staged')
        runtime.client.connect.assert_called_once()
        starts=[call for call in runtime.client.call.call_args_list if call.args[0]=='turn.start']
        self.assertEqual(len(starts),1);finish.assert_called_once()


if __name__=='__main__':unittest.main()
