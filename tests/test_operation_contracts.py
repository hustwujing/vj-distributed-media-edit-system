import unittest
from unittest.mock import patch
from webapp.operations import InputRef,for_image,get,catalog
from webapp.workflows import Step,validate_plan
from webapp.quality_policy import Acceptance
from webapp.execution_contracts import profile,dispatch_kind,classify_error

class ContractTests(unittest.TestCase):
    def test_sam3_text_constraints_are_enforced_before_execution_and_discoverable(self):
        from webapp.operations import validate_parameters
        inputs={'source':{'asset':'owned'}}
        for prompt in ('宇航员','x'*121):
            with self.assertRaises(ValueError):validate_parameters('image.segment_sam3',{'prompt':prompt},inputs)
        self.assertEqual(validate_parameters('image.segment_sam3',{'prompt':'astronaut'},inputs)['prompt'],'astronaut')
        constraints=get('image.segment_sam3').public()['constraints']
        self.assertEqual(constraints['text_language'],'English')
        self.assertEqual(constraints['text_max_length'],120)
    def test_image_input_validation_uses_adapter_contract_values(self):
        from webapp.image_providers import check_image_request,EDITOR_CONTRACT
        small={'input_min_side':16,'input_max_pixels':2048,'maximum_images_including_selection_guide':2,'assembled_prompt_max_length':20}
        check_image_request(small,[(32,16)],'edit')
        with self.assertRaises(ValueError):check_image_request(EDITOR_CONTRACT,[(32,16)],'edit')
        for sizes,prompt in [([(15,16)],'edit'),([(64,64)],'edit'),([(16,16)]*3,'edit'),([(16,16)],'x'*21)]:
            with self.assertRaises(ValueError):check_image_request(small,sizes,prompt)
        self.assertEqual(get('image.erase').public()['constraints'],EDITOR_CONTRACT)

    def test_registered_operations_have_stable_resources_and_roles(self):
        self.assertTrue(for_image('remove_bg').gpu)
        self.assertFalse(for_image('generate').retry_safe)
        self.assertEqual(for_image('generate').id,'image.edit')
        self.assertEqual(len({o['id'] for o in catalog()['operations']}),len(catalog()['operations']))
        self.assertEqual(get('video.generate_from_image').outputs,('video',))
    def test_provider_reference_size_constraint_is_checked_before_submission(self):
        from webapp.workflow_graph import Task,validate_operation_input_constraints
        common={'type':'operation','name':'generate','objective':'Generate video','operation':'video.generate_from_image',
                'implementation':'generation_async@1','parameters':{'prompt':'push in','width':1280,'height':720,'duration':5},
                'inputs':{'source':{'asset':'portrait'}},'acceptance':[{'kind':'media','video':True}]}
        task=Task.model_validate({'id':'generate',**common})
        assets={'portrait':{'metadata':{'kind':'image','width':512,'height':512}}}
        environment={'VIDEO_USE_IMAGE_VIDEO_BACKEND':'wan','VIDEO_USE_IMAGE_VIDEO_URL':'http://127.0.0.1:9898',
                     'VIDEO_USE_IMAGE_VIDEO_KEY':'test','VIDEO_USE_IMAGE_VIDEO_MODEL':'wan2.6-i2v'}
        with patch.dict('os.environ',environment,clear=False),self.assertRaisesRegex(ValueError,'preprocessing Task'):
            validate_operation_input_constraints(task,{'generate':task},assets)
        assets['portrait']['metadata'].update(width=1280,height=720)
        with patch.dict('os.environ',environment,clear=False):validate_operation_input_constraints(task,{'generate':task},assets)
    def test_reviewer_report_cannot_be_a_delivery_dependency(self):
        from webapp.workflow_graph import Task,validate_delivery_dependencies
        review=Task.model_validate({'id':'review','type':'agent','profile':'visual_quality_reviewer','name':'Review',
            'objective':'Review output','outputs':['review.json'],'output_roles':{'review_report':'review.json'},
            'acceptance':[{'kind':'json','file':'review.json'}]})
        delivery=Task.model_validate({'id':'deliver','type':'agent','profile':'editing_implementer','name':'Deliver',
            'objective':'Deliver the accepted image','inputs':{'review_report':{'step':'review','role':'review_report'}},
            'outputs':['final.png'],'output_roles':{'primary_image':'final.png'},
            'acceptance':[{'kind':'image','file':'final.png','stage':'final'}],'delivery_stage':True})
        with self.assertRaisesRegex(ValueError,'evidence, not deliverable inputs'):
            validate_delivery_dependencies(delivery,{'review':review,'deliver':delivery})
    def test_references_cannot_mix_asset_and_step_or_omit_role(self):
        for value in ({'asset':'a','step':'s','role':'image'},{'step':'s'},{'url':'https://host/private'},{}):
            with self.assertRaises(ValueError):InputRef.model_validate(value)
    def test_operation_has_no_caller_executable_or_unsafe_retry(self):
        for payload in ({'operation':'image.edit','retry_safe':True},{'operation':'image.remove_background','argv':['sh']},{'operation':'unknown'}):
            with self.assertRaises(ValueError):Step.model_validate({'id':'s','name':'s','kind':'operation','inputs':{'source':{'asset':'a'}},**payload})
    def test_only_canonical_step_kinds_are_accepted_and_persisted(self):
        common={'id':'s','name':'s','argv':['python','edit/a.py'],'outputs':['a.png']}
        self.assertEqual(Step.model_validate({**common,'kind':'script'}).model_dump()['kind'],'script')
        for kind in ('local','image'):
            with self.assertRaises(ValueError):Step.model_validate({**common,'kind':kind})
        with self.assertRaises(ValueError):Step.model_validate({**common,'kind':'script','image':None})
        self.assertNotIn('legacy_step_kinds',catalog())
        from webapp.operations import for_step
        for kind in ('local','image'):
            with self.assertRaises(ValueError):for_step({'kind':kind})
    def test_new_acceptance_and_legacy_are_equivalent(self):
        self.assertEqual(Acceptance(target='image',reviewer='agent',criterion='correct').kind,'agent')
        self.assertEqual(Acceptance(kind='review',criterion='choice').reviewer,'user')
        with self.assertRaises(ValueError):Acceptance(kind='file',target='image',reviewer='agent',criterion='correct')
        with self.assertRaises(ValueError):Acceptance(kind='text',has_alpha=True,contains=['x'])
    def test_media_edit_title_background_is_typed(self):
        from webapp.operations import validate_parameters
        inputs={'source':{'asset':'clip'}}
        plan={'clips':[{'asset':'source'}],'titles':[{'text':'title','background':True}]}
        value=validate_parameters('media.edit',{'edit_plan':plan},inputs)
        self.assertIs(value['edit_plan']['titles'][0]['background'],True)
        plan['titles'][0]['background']='#000000'
        with self.assertRaises(ValueError):validate_parameters('media.edit',{'edit_plan':plan},inputs)
    def test_profile_does_not_depend_on_user_conversation_mode(self):
        self.assertEqual(profile('execute',review=True),'review')
        self.assertEqual(dispatch_kind({'workflow_status':'reviewing'}),'agent')
        self.assertEqual(dispatch_kind({'workflow_status':'ready'}),'workflow')
        self.assertEqual(classify_error('SyntaxError','local'),'code')
    def test_unavailable_dependency_is_not_reclassified_as_agent_failure(self):
        from webapp.workflow_executor import failure_error_code
        detail={'code':'result_unavailable','error_type':'ArtifactUnavailable'}
        classification=classify_error(detail,'local')
        self.assertEqual(classification,'result_unavailable')
        self.assertEqual(failure_error_code({'type':'agent'},'local',[],classification),'result_unavailable')
    def test_dependencies_must_match_input_references(self):
        with self.assertRaisesRegex(ValueError,'depends_on'):
            validate_plan({'key':'x','name':'x','steps':[{'id':'s','name':'s','kind':'operation','operation':'image.remove_background','inputs':{'source':{'step':'before','role':'foreground'}},'goal':'cutout','on_failure':'pause','acceptance':[{'kind':'agent','criterion':'subject'}]}]})

class InputResolutionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_workflows import WorkflowTests
        self.f=WorkflowTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_role_and_hash_bind_approved_dependency(self):
        from pathlib import Path
        from webapp.workflow_inputs import resolve,digest
        from webapp import workflows
        from tests.test_workflows import local_step
        step=local_step();step['output_roles']={'primary_image':'final.png'}
        second=local_step('second',['package']);second['inputs']={'source':{'step':'package','role':'primary_image'}}
        self.f.submit([step,second]);self.f.tick()
        w=self.f.get();root=self.f.store.project_dir(self.f.project)/'agent/workspace';path=root/w['steps'][0]['result'][0]
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'validated')
        workflows.change(self.f.store,w['id'],'package',quality={'files':[{'name':w['steps'][0]['result'][0],'sha256':digest(path),'kind':'image','width':8,'height':8}],'output_roles':{'primary_image':w['steps'][0]['result'][0]}})
        w=self.f.get();env={'workspace':root,'sources':root/'sources','assets':[]}
        bindings=resolve(self.f.store,w,w['steps'][1],env)
        self.assertEqual(bindings['source']['revision'],0)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'验收版本'):resolve(self.f.store,self.f.get(),self.f.get()['steps'][1],env)
    def test_ambiguous_output_not_silently_selected(self):
        from webapp.workflow_inputs import roles
        with self.assertRaisesRegex(ValueError,'多个候选'):roles({'kind':'operation','operation':'image.edit'},['a.png','b.png'])
    def test_plain_operation_result_is_bound_to_registered_role_before_validation(self):
        from webapp.workflow_executor import bind_output_roles
        step={'spec':{'kind':'operation','operation':'image.batch_export'}}
        files,roles=bind_output_roles(step,['edit/output/image.webp'])
        self.assertEqual(roles,{'primary_image':'edit/output/image.webp'})
        self.assertEqual(files.roles,roles)

class IntentTests(unittest.TestCase):
    def test_multi_stage_cannot_declare_single(self):
        from webapp.execution_intent import Intent
        with self.assertRaises(ValueError):Intent(route='single',operations=['image.remove_background','script'],goal='combine',estimated_seconds=30)
        with self.assertRaises(ValueError):Intent(route='single',operations=['image.remove_background'],goal='cutout',estimated_seconds=300)
    def test_confirmed_workflow_blocks_direct_tool_before_submission(self):
        from tests.test_workflows import WorkflowTests
        from webapp.execution_intent import declare,enforce_direct
        from types import SimpleNamespace
        fixture=WorkflowTests();fixture.setUp()
        try:
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.turn,))
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.plan,))
            declare(fixture.store,fixture.plan,{'route':'workflow','operations':['image.remove_background','script'],'goal':'compose','estimated_seconds':300})
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.plan,))
            with self.assertRaisesRegex(ValueError,'工作流'):enforce_direct(SimpleNamespace(kind='agent',store=fixture.store,run_id=fixture.turn),'image.remove_background')
            with self.assertRaises(ValueError):declare(fixture.store,fixture.plan,{'route':'single','operations':['image.remove_background'],'goal':'change','estimated_seconds':20})
        finally:fixture.tearDown()

class ProviderCapabilityTests(unittest.TestCase):
    def test_unsupported_duration_is_rejected_before_submission(self):
        from unittest.mock import patch
        from webapp.generation_provider import validate
        caps={'operations':{'video.generate_from_image':{'durations':[5,10],'reference_image':True,'sizes':[[1280,720]]}}}
        with patch('webapp.generation_provider.capabilities',return_value=caps):
            with self.assertRaisesRegex(ValueError,'时长'):validate('video.generate_from_image',{'duration':15,'width':1280,'height':720})
    def test_native_image_size_cannot_be_claimed_as_guaranteed(self):
        from unittest.mock import patch
        import os
        with patch.dict(os.environ,{'VIDEO_USE_MODEL_BASE_URL':'https://example.test/v1','VIDEO_USE_MODEL_API_KEY':'test-only-key','VIDEO_USE_GENERATION_URL':''}):
            step={'id':'image','name':'image','kind':'operation','operation':'image.generate_from_text','goal':'icon','on_failure':'pause',
                  'parameters':{'prompt':'blue icon','width':1024,'height':1024},'acceptance':[{'kind':'image','width':1024}]}
            with self.assertRaisesRegex(ValueError,'尺寸不保证'):validate_plan({'key':'image','name':'image','steps':[step]})
            step['parameters']['fit']='contain'
            self.assertEqual(validate_plan({'key':'image','name':'image','steps':[step]}).steps[0].parameters['fit'],'contain')

class CompletionIntentTests(unittest.TestCase):
    def test_declared_workflow_cannot_finish_without_submission(self):
        from tests import test_workflows as fixtures
        from webapp.execution_intent import declare,completion_problem
        fixture=fixtures.WorkflowTests();fixture.setUp()
        try:
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.turn,))
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.plan,))
            declare(fixture.store,fixture.plan,{'route':'workflow','operations':['script'],'goal':'two-stage work','estimated_seconds':300})
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.plan,))
            self.assertEqual(completion_problem(fixture.store,fixture.turn,[{'name':'script.py'}])[0],'paused')
        finally:fixture.tearDown()
    def test_single_operation_requires_a_result(self):
        from tests import test_workflows as fixtures
        from webapp.execution_intent import declare,completion_problem
        fixture=fixtures.WorkflowTests();fixture.setUp()
        try:
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.turn,))
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.plan,))
            declare(fixture.store,fixture.plan,{'route':'single','operations':['image.local_adjust'],'goal':'adjust','estimated_seconds':30})
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.plan,))
            self.assertEqual(completion_problem(fixture.store,fixture.turn,[])[0],'failed')
            self.assertEqual(completion_problem(fixture.store,fixture.turn,[{'name':'edit_plan.json'},{'name':'source_candidate.wav'}])[0],'failed')
            self.assertEqual(completion_problem(fixture.store,fixture.turn,[{'name':'final.png','quality_state':'pending'}])[0],'failed')
            self.assertIsNone(completion_problem(fixture.store,fixture.turn,[{'name':'result.png'}]))
        finally:fixture.tearDown()

    def test_denied_service_cannot_be_hidden_by_an_error_report(self):
        from tests import test_workflows as fixtures
        from webapp.execution_intent import declare,completion_problem,enforce_direct
        from types import SimpleNamespace
        fixture=fixtures.WorkflowTests();fixture.setUp()
        try:
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.turn,))
            fixture.store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(fixture.plan,))
            declare(fixture.store,fixture.plan,{'route':'single','operations':['script'],'goal':'transcribe','estimated_seconds':90})
            fixture.store.execute("UPDATE agent_turns SET status='succeeded' WHERE id=?",(fixture.plan,))
            with self.assertRaisesRegex(ValueError,'audio.transcribe'):
                enforce_direct(SimpleNamespace(kind='agent',store=fixture.store,run_id=fixture.turn),'audio.transcribe')
            self.assertEqual(completion_problem(fixture.store,fixture.turn,[{'name':'transcription-error.json'}])[0],'paused')
            events=fixture.store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind='operation_denied'",(fixture.turn,))
            self.assertEqual(len(events),1)
        finally:fixture.tearDown()
