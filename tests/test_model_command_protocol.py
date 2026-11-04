import copy,json,unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext
from types import SimpleNamespace
from jsonschema import ValidationError
from webapp import workflow_graph as graph
from webapp.workflow_model_protocol import manager_schema,compile_manager,ToolProtocol,CommandError,schema_complexity,repair_options
from webapp.strict_schema import obj,schema_for,transform
from webapp.model_tool_protocol import request,response,strict_unsupported
from webapp.model_adapters import get,PublicResponseStructureError
from webapp.specialist_profiles import get as profile


def frozen(task=None,user=False):
    spec={'id':'render','type':'script','name':'render','objective':'Write report','argv':['python','-c','pass'],
          'outputs':['report.txt'],'output_roles':{'report':'report.txt'},'acceptance':[{'kind':'file'}],
          'basic_reason':'local report','delivery_stage':True}
    return {'graph_version':3,'authorization_version':2,'snapshot_digest':'a'*64,'planning_request_id':'request-a',
            'tasks':[] if task is False else [{'id':'render','status':'paused','revision':2,'spec':graph.Task.model_validate(task or spec).model_dump()}],
            'dependencies':[],'trigger':{'task_id':'render'},'input_batch':{'through_sequence':7,'inputs':[{'context':{'allowed_revision_tasks':['render']}}]} if user else None}


class ModelCommandTests(unittest.TestCase):
    def test_changes_preserve_frozen_contract_and_bind_original_request(self):
        f=frozen();command={'kind':'APPLY_PLAN','reason':'Repair script','plan':{'changes':[{'task_id':'render','set':{'argv':['python','-c','print(1)']}}],'continuation':{'mode':'complete'}}}
        schema=manager_schema(f);wire=transform(command,schema,encode=True);result=compile_manager(transform(wire,schema),f)
        revised=result['proposal']['revise_tasks'][0]
        self.assertEqual(revised,{**f['tasks'][0]['spec'],'argv':['python','-c','print(1)']})
        self.assertEqual(result['snapshot_digest'],'a'*64);self.assertEqual(result['base_graph_version'],3)
        self.assertEqual(result['proposal']['proposal_id'],'request-a');self.assertEqual(result['input_through_sequence'],0)
        self.assertNotIn('snapshot_digest',schema['properties'])
        command['plan']['changes'][0]['set']['acceptance']=[]
        with self.assertRaises(CommandError):compile_manager(command,f)
        with self.assertRaises(ValidationError):transform(command,schema,encode=True)

    def test_user_feedback_has_only_its_authorized_edit_fields(self):
        f=frozen(user=True);schema=manager_schema(f)
        cmd={'kind':'APPLY_PLAN','reason':'User feedback','plan':{'changes':[{'task_id':'render','set':{'objective':'Updated goal'}}],'continuation':{'mode':'complete'}}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)
        self.assertEqual(result['input_through_sequence'],7)
        self.assertEqual(result['proposal']['revise_tasks'][0]['objective'],'Updated goal')
        cmd['plan']['changes'][0]['set']['output_roles']={'wrong':'report.txt'}
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_automatic_replacement_inherits_exact_requirement_without_model_retyping(self):
        f=frozen();schema=manager_schema(f)
        implementation={'name':'New implementation','type':'script','argv':['python','-c','print(1)'],
                        'files':[{'path':'report.txt','roles':['report']}],'basic_reason':'Local repair'}
        cmd={'kind':'APPLY_PLAN','reason':'Replace failed implementation','plan':{
            'repair_replacements':[{'task_id':'render','implementation':implementation}], 'continuation':{'mode':'wait'}}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)['proposal']
        replacement=result['replacements']['render'];self.assertTrue(replacement.startswith('render_r_'))
        self.assertEqual(result['add_tasks'][0]['id'],replacement)
        self.assertEqual(result['replan_after'],[replacement])
        for key in ('objective','acceptance','semantics','delivery_stage'):
            self.assertEqual(result['add_tasks'][0][key],f['tasks'][0]['spec'][key])
        implementation['acceptance']=[]
        with self.assertRaises(ValidationError):transform(cmd,schema,encode=True)
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_output_declaration_has_one_source_and_continuation_is_exclusive(self):
        f=frozen(False);schema=manager_schema(f)
        task={'id':'render','type':'script','name':'render','objective':'Write','argv':['python','-c','pass'],
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file'}],'basic_reason':'local','delivery_stage':True}
        cmd={'kind':'COMPLETE','reason':'Done planning','plan':{'new_tasks':[task],'continuation':{'mode':'complete'}}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)
        self.assertEqual(result['proposal']['add_tasks'][0]['outputs'],['report.txt']);self.assertEqual(result['proposal']['replan_after'],[])
        cmd['plan']['continuation']['tasks']=['render']
        with self.assertRaises(ValidationError):transform(cmd,schema,encode=True)
        cmd['plan']['continuation']={'mode':'complete'};task['files'].append({'path':'other.txt','roles':['report']})
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_host_derives_input_dependencies_and_waits_for_batch_leaves(self):
        f=frozen(False);schema=manager_schema(f)
        first={'id':'prepare','type':'agent','profile':'editing_implementer','name':'Prepare','objective':'Prepare image',
               'files':[{'path':'prepared.png','roles':['primary_image']}],'acceptance':[{'kind':'image'}],'delivery_stage':False}
        final={'id':'deliver','type':'agent','profile':'editing_implementer','name':'Deliver','objective':'Deliver image',
               'input_bindings':[{'role':'source','ref':{'step':'prepare','role':'primary_image'}}],
               'files':[{'path':'final.png','roles':['primary_image']}],
               'acceptance':[{'kind':'image'}],'delivery_stage':True}
        cmd={'kind':'APPLY_PLAN','reason':'Typed batch','plan':{'new_tasks':[first,final],
             'continuation':{'mode':'wait'}}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)['proposal']
        self.assertEqual(result['add_dependencies'],[{'upstream':'prepare','downstream':'deliver'}])
        self.assertEqual(result['replan_after'],['deliver'])

    def test_agent_input_bindings_compile_to_one_authoritative_map(self):
        f=frozen(False);schema=manager_schema(f)
        task={'id':'deliver','type':'agent','profile':'editing_implementer','name':'Deliver','objective':'Deliver image',
              'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],
              'files':[{'path':'final.png','roles':['primary_image']}],'acceptance':[{'kind':'image'}],'delivery_stage':True}
        command={'kind':'COMPLETE','reason':'Deliver','plan':{'new_tasks':[task],'continuation':{'mode':'complete'}}}
        result=compile_manager(transform(transform(command,schema,encode=True),schema),f)['proposal']['add_tasks'][0]
        self.assertEqual(result['inputs'],{'source':{'asset':'asset','step':None,'role':None,'repair_candidate':None}})
        task['input_bindings'].append({'role':'source','ref':{'asset':'asset2'}})
        with self.assertRaises(CommandError):compile_manager(transform(transform(command,schema,encode=True),schema),f)

    def test_planner_normalizes_only_unambiguous_legacy_task_shapes(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.evidence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op},draft={'status':'OPEN'})
        arguments={'tasks':[{'id':'evidence','type':'operation','name':'Evidence','objective':'Inspect audio',
            'operation':'media.evidence','purpose':'evidence','parameters':{'mode':'audio'},
            'inputs':[{'role':'source','ref':{'asset':'asset'}}],
            'acceptance':[{'kind':'json','file':'invented.json'}]}],
            'kind':'APPLY_PLAN','reason':'Inspect','continuation':{'mode':'wait','tasks':['evidence']}}
        result=p.decode('submit_typed_plan',arguments)
        self.assertEqual(result['command']['tasks'][0]['inputs']['source']['asset'],'asset')
        self.assertEqual(result['command']['tasks'][0]['acceptance'][0]['file'],'invented.json')
        self.assertNotIn('tasks',result['command']['continuation'])
        self.assertEqual(set(result['_normalizations']),{'continuation.tasks_removed','tasks.0.operation_inputs_mapped'})

    def test_planner_normalizes_common_flat_operation_input_bindings(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.concat_sequence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op},draft={'status':'OPEN'})
        base={'id':'concat','type':'operation','name':'Concat','objective':'Join clips','operation':'media.concat_sequence',
              'parameters':{'roles':['clip_1'],'audio_mode':'preserve','threads':2},'delivery_stage':True}
        for inputs in ([{'name':'clip_1','asset':'asset'}],[{'clip_1':{'asset':'asset'}}]):
            with self.subTest(inputs=inputs):
                arguments={'tasks':[{**base,'inputs':inputs}],'kind':'APPLY_PLAN','reason':'Join','continuation':{'mode':'wait'}}
                result=p.decode('submit_typed_plan',arguments)
                self.assertEqual(result['command']['tasks'][0]['input_bindings'],[{'role':'clip_1','ref':{'asset':'asset'}}])

    def test_noncut_transition_default_matches_runtime_contract(self):
        from webapp.operation_parameters import EditTransition
        self.assertEqual(EditTransition(type='cut').duration,0)
        self.assertEqual(EditTransition(type='fade').duration,.4)
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):EditTransition(type='fade',duration=0)

    def test_non_strict_wire_may_omit_optional_fields_but_not_required_fields(self):
        schema=obj({'required':{'type':'string'},'optional':{'type':'integer'}},['required'])
        self.assertEqual(transform({'required':'ok'},schema),{'required':'ok'})
        with self.assertRaises(ValidationError):transform({},schema)

    def test_image_operation_options_are_typed_and_reject_duplicate_parameter_wrappers(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='image.local_adjust')
        f=frozen(False);f['operations']=[op]
        schema=manager_schema(f,{op['id']:op})
        task={'id':'adjust','type':'operation','name':'Adjust','objective':'Adjust image',
              'operation':'image.local_adjust','parameters':{'parameters':{'brightness':1.1}},
              'inputs':{'source':{'asset':'asset'}},'delivery_stage':True}
        command={'kind':'COMPLETE','reason':'Adjust','plan':{'new_tasks':[task],'continuation':{'mode':'complete'}}}
        self.assertEqual(transform(transform(command,schema,encode=True),schema)['plan']['new_tasks'][0]['parameters']['parameters']['brightness'],1.1)
        task['parameters']={'parameters':{'parameters':{'brightness':1.1}}}
        with self.assertRaises(ValidationError):transform(command,schema,encode=True)

    def test_arbitrary_json_roundtrip_and_duplicate_map_rejection(self):
        schema=obj({'parameters':{'type':'object','additionalProperties':{}},'optional':{'type':'integer'}},['parameters'])
        native={'parameters':{'nested':{'entries':[{'x':True},None,1.5]},'word':'hello'}}
        wire=transform(native,schema,encode=True);self.assertIsNone(wire['optional'])
        self.assertEqual(transform(wire,schema),native)
        wire['parameters'].append(copy.deepcopy(wire['parameters'][0]))
        with self.assertRaisesRegex(ValueError,'Duplicate map'):transform(wire,schema)

    def test_acceptance_wire_schema_excludes_fields_for_other_output_types(self):
        schema=manager_schema(frozen(False))
        image=schema['$defs']['CommandAcceptanceImage']['properties']
        media=schema['$defs']['CommandAcceptanceMedia']['properties']
        self.assertNotIn('decode',image);self.assertNotIn('fps',image)
        self.assertIn('decode',media);self.assertNotIn('has_alpha',media)
        self.assertNotIn('target',image);self.assertNotIn('reviewer',image)
        self.assertIn('criterion',schema['$defs']['CommandAcceptanceAgent']['required'])

    def test_schema_feedback_names_the_actual_wrong_variant_field_without_values(self):
        from webapp.strict_schema import validation_feedback
        schema=manager_schema(frozen(False));command={'kind':'COMPLETE','reason':'Plan','plan':{'new_tasks':[{
            'id':'render','type':'script','name':'render','objective':'Write','argv':['python','-c','pass'],
            'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file'}],'basic_reason':'local','delivery_stage':True}],
            'continuation':{'mode':'complete'}}}
        wire=transform(command,schema,encode=True)
        wire['plan']['new_tasks'][0]['profile']='PRIVATE_VALUE'
        try:transform(wire,schema)
        except ValidationError as exc:
            details=validation_feedback(exc)
            self.assertEqual(exc.transport_stage,'wire')
            self.assertTrue(any(x['path']=='plan.new_tasks.0.profile' for x in details),details)
            self.assertNotIn('PRIVATE_VALUE',str(details))
        else:self.fail('An extra field was accepted')
        del wire['plan']['new_tasks'][0]['profile']
        wire['plan']['new_tasks'][0]['preflight']=['python']
        try:transform(wire,schema)
        except ValidationError as exc:
            self.assertTrue(any(x['path']=='plan.new_tasks.0.preflight.0' for x in validation_feedback(exc)))
        else:self.fail('A package name was accepted as a workspace preflight file')

    def test_finalize_does_not_advertise_inspection_or_contract(self):
        f=frozen();f['inspectable_refs']=['asset'];f['operations']=[{'id':'image.local_adjust'}]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},finalize=True)
        self.assertEqual(list(p.schemas),['submit_plan'])
        with self.assertRaises(CommandError):p.decode('inspect',{'refs':[]})

    def test_contract_lookup_batches_distinct_frozen_operation_ids(self):
        f=frozen(False);f['operations']=[{'id':'media.edit'},{'id':'audio.process'}]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        source=p.schemas['contract'];wire=transform({'operations':['media.edit','audio.process']},source,encode=True)
        self.assertEqual(p.decode('contract',wire)['operations'],['media.edit','audio.process'])
        with self.assertRaises(ValidationError):transform({'operations':['media.edit','media.edit']},source,encode=True)

    def test_plan_draft_uses_typed_commands_with_bounded_schema_complexity(self):
        f=frozen(False);p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertEqual(list(p.schemas),['submit_typed_plan','continue_run','request_user_input','request_authorization','new_run_required'])
        self.assertNotIn('commit_plan',p.schemas)
        self.assertEqual(p.context()['model_command_contract']['version'],'plan-draft-v1')
        self.assertNotIn('submit_plan',p.schemas)
        metrics=schema_complexity(p.definitions())
        self.assertLess(metrics['schema_bytes'],512000)
        self.assertLess(metrics['schema_max_depth'],80)
        self.assertLess(metrics['schema_union_branches'],1500)
        from webapp.workflow_agents import model_output_limit
        self.assertEqual(model_output_limit(profile('media_planner'),p),6144)
        final=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'},finalize=True)
        self.assertEqual(model_output_limit(profile('media_planner'),final),1024)

    def test_authorization_request_binds_the_existing_goal_in_the_host(self):
        f=frozen(False);f['goal']='Original immutable goal';f['scope_confirmation_required']=True
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        source=p.schemas['request_authorization'];definition=source['$defs']['AuthorizationExpansion']
        self.assertNotIn('goal',definition['properties'])
        current=graph.Authorization(goal=f['goal'],allowed_assets=[],allowed_operations=[],delivery_targets=['artifact'],content_fingerprint='b'*64)
        payload=current.model_dump();payload.pop('goal')
        wire=transform({'reason':'Need expanded scope','authorization':payload},source,encode=True)
        result=p.decode('request_authorization',wire)['proposal']
        self.assertEqual(result['authorization']['goal'],f['goal'])

    def test_agent_repair_projects_same_contract_revision_as_explicit_option(self):
        task={'id':'review','type':'agent','profile':'editing_implementer','name':'Review repair','objective':'Recheck output',
              'outputs':['report.txt'],'output_roles':{'report':'report.txt'},'acceptance':[{'kind':'file'}],
              'basic_reason':'Recheck with failure context','delivery_stage':False}
        f=frozen(task);f['tasks'][0]['status']='paused';f['tasks'][0]['id']='review';f['trigger']={'task_id':'review'}
        options=repair_options(f)
        self.assertIn({'action':'rerun_agent_revision','task_id':'review','changes_set':{},
            'meaning':'Create a new immutable revision with the same Agent contract and current repair context.'},options)
        protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertEqual(protocol.context()['model_command_contract']['repair_options'],options)

    def test_loaded_operation_contract_exposes_required_roles_and_parameters(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.evidence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        command={'kind':'APPLY_PLAN','reason':'Collect audio evidence','plan':{'new_tasks':[{
            'id':'evidence','type':'operation','name':'Evidence','objective':'Inspect authorized audio',
            'operation':'media.evidence','purpose':'evidence','parameters':{'mode':'audio'},'inputs':{'source':{'asset':'asset'}}}],
            'continuation':{'mode':'wait'}}}
        wire=transform(command,p.schemas['submit_plan'],encode=True)
        result=p.decode('submit_plan',wire)['proposal']['proposal']['add_tasks'][0]
        self.assertEqual(result['inputs']['source']['asset'],'asset');self.assertEqual(result['implementation'],op['implementation'])
        command['plan']['new_tasks'][0]['inputs']={'primary_image':{'asset':'asset'}}
        with self.assertRaises(ValidationError):transform(command,p.schemas['submit_plan'],encode=True)
        unread=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'})
        command['plan']['new_tasks'][0]['inputs']={'source':{'asset':'asset'}}
        with self.assertRaises(ValidationError):transform(command,unread.schemas['submit_plan'],encode=True)

    def test_operation_command_inherits_server_owned_technical_acceptance(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.concat_sequence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        draft={'id':'concat','type':'operation','name':'Join clips','objective':'Join the authorized clips',
               'operation':'media.concat_sequence','parameters':{'roles':['clip_1'],'audio_mode':'preserve','threads':2},
               'input_bindings':[{'role':'clip_1','ref':{'asset':'asset'}}],'delivery_stage':True}
        wire=transform({'kind':'COMPLETE','reason':'Join clips','plan':{'new_tasks':[draft],
            'continuation':{'mode':'complete'}}},p.schemas['submit_plan'],encode=True)
        task=p.decode('submit_plan',wire)['proposal']['proposal']['add_tasks'][0]
        self.assertEqual([item['kind'] for item in task['acceptance']],['media','json'])
        self.assertTrue(task['acceptance'][0]['decode'])
        self.assertIn('registered operation',task['basic_reason'])

    def test_operation_business_acceptance_is_preserved_without_model_owned_filename(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.concat_sequence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        draft={'id':'concat','type':'operation','name':'Join','objective':'Join clips','operation':'media.concat_sequence',
               'parameters':{'roles':['clip_1'],'audio_mode':'preserve','threads':2},
               'input_bindings':[{'role':'clip_1','ref':{'asset':'asset'}}],'delivery_stage':True,
               'acceptance':[{'kind':'media','file':'guessed.mp4','width':640,'height':360,'min_duration':11.8,'max_duration':12.2,'video':True}]}
        command={'kind':'COMPLETE','reason':'Join','plan':{'new_tasks':[draft],'continuation':{'mode':'complete'}}}
        task=p.decode('submit_plan',transform(command,p.schemas['submit_plan'],encode=True))['proposal']['proposal']['add_tasks'][0]
        business=task['acceptance'][0]
        self.assertEqual((business['file'],business['source'],business['width'],business['height']),('*','user',640,360))
        self.assertTrue(any(check['source']=='technical' for check in task['acceptance']))

    def test_operation_alternative_outputs_select_one_parameter_bound_branch(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.edit')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        base={'id':'edit','type':'operation','name':'Edit','objective':'Edit video','operation':'media.edit',
              'parameters':{'edit_plan':{'clips':[{'asset':'source'}],'output':{'audio_only':False}}},
              'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],'delivery_stage':True}
        command={'kind':'COMPLETE','reason':'Edit','plan':{'new_tasks':[base],'continuation':{'mode':'complete'}}}
        task=p.decode('submit_plan',transform(command,p.schemas['submit_plan'],encode=True))['proposal']['proposal']['add_tasks'][0]
        self.assertIn(True,[check.get('video') for check in task['acceptance']])
        self.assertNotIn(False,[check.get('video') for check in task['acceptance']])
        base['parameters']['edit_plan']['output']['audio_only']=True
        task=p.decode('submit_plan',transform(command,p.schemas['submit_plan'],encode=True))['proposal']['proposal']['add_tasks'][0]
        self.assertIn(False,[check.get('video') for check in task['acceptance']])
        self.assertNotIn(True,[check.get('video') for check in task['acceptance']])

    def test_user_agent_revision_declares_outputs_once_with_files(self):
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Export','objective':'Export PNG',
              'outputs':['final.png'],'output_roles':{'primary_image':'final.png'},
              'acceptance':[{'kind':'image','file':'final.png'}],'basic_reason':'Local export','delivery_stage':True}
        f=frozen(task,user=True);schema=manager_schema(f)
        command={'kind':'APPLY_PLAN','reason':'Add requested JPEG','plan':{'changes':[{'task_id':'render','set':{
            'objective':'Export PNG and JPEG','files':[{'path':'final.png','roles':['primary_image']},{'path':'final.jpg','roles':['alternate_image']}],
            'acceptance':[{'kind':'image','file':'final.png'},{'kind':'image','file':'final.jpg'}]}}],
            'continuation':{'mode':'wait'}}}
        result=compile_manager(transform(transform(command,schema,encode=True),schema),f)['proposal']['revise_tasks'][0]
        self.assertEqual(result['outputs'],['final.png','final.jpg'])
        self.assertEqual(result['output_roles'],{'primary_image':'final.png','alternate_image':'final.jpg'})

    def test_review_uses_only_actual_inspection_ids_and_host_snapshot(self):
        from webapp.workflow_agent_contracts import ReviewDecision
        f=frozen();f.update(result_schema=ReviewDecision.model_json_schema(),review_scope='task')
        p=ToolProtocol(profile('delivery_reviewer'),f,{'control_kind':'REVIEW'},inspections={'b'*32:'immutable.png'})
        value={'verdict':'pass','evidence':[{'criterion_index':0,'observation':'Checked','inspection_id':'b'*32}]}
        wire=transform(value,p.schemas['submit_result'],encode=True)
        result=p.decode('submit_result',wire)
        self.assertEqual(result['decision']['snapshot_digest'],'a'*64)
        wire['evidence'][0]['inspection_id']='c'*32
        with self.assertRaises(ValidationError):p.decode('submit_result',wire)

    def test_final_review_selects_short_candidate_keys_not_internal_ids_or_paths(self):
        from webapp.workflow_agent_contracts import ReviewDecision
        f=frozen();f.update(result_schema=ReviewDecision.model_json_schema(),review_scope='final')
        inspections={'b'*32:'edit/final.png'};keys={'delivery_1':'b'*32}
        f['tasks'][0].update(accepted_result=['edit/final.png'],quality={'output_roles':{'primary_image':'edit/final.png'}})
        p=ToolProtocol(profile('delivery_reviewer'),f,{'control_kind':'REVIEW'},inspections=inspections,candidate_keys=keys)
        value={'verdict':'pass','evidence':[{'criterion_index':0,'observation':'Checked','inspection_id':'b'*32}],
               'deliverables':['delivery_1'],'primary':'delivery_1'}
        wire=transform(value,p.schemas['submit_result'],encode=True)
        self.assertEqual(p.decode('submit_result',wire)['decision']['primary'],'delivery_1')
        exact=transform({**value,'deliverables':['edit/final.png'],'primary':'edit/final.png'},p.schemas['submit_result'],encode=True)
        self.assertEqual(p.decode('submit_result',exact)['decision']['primary'],'edit/final.png')
        role=transform({**value,'deliverables':['primary_image'],'primary':'primary_image'},p.schemas['submit_result'],encode=True)
        self.assertEqual(p.decode('submit_result',role)['decision']['primary'],'primary_image')
        for invalid in ('b'*32,'edit/other.png'):
            wire=transform(value,p.schemas['submit_result'],encode=True);wire['primary']=invalid
            with self.assertRaises(ValidationError):p.decode('submit_result',wire)

    def test_gateway_fallback_is_explicit_and_never_auth_or_generic_failure(self):
        self.assertTrue(strict_unsupported(400,{'error':{'code':'unsupported_parameter','param':'tools.0.strict'}}))
        self.assertTrue(strict_unsupported(422,{'error':{'code':'strict_not_supported'}}))
        for status in (401,403,429,500,502):
            self.assertFalse(strict_unsupported(status,{'error':{'code':'strict_not_supported'}}))
        for payload in ({'error':{'type':'invalid_request_error'}},{'error':{'code':'unsupported_parameter','param':'model'}},
                        {'error':{'message':'strict is not supported'}}):
            self.assertFalse(strict_unsupported(400,payload))

    def test_nonstrict_explicitly_disables_responses_but_omits_other_extensions(self):
        definitions=[{'name':'submit_plan','description':'Synthetic','schema':obj({'ok':{'type':'boolean'}})}]
        for identifier in ('responses','chat_completions','anthropic'):
            adapter=get(identifier);body=adapter.conversation('rules','question')
            strict=request(adapter,body,definitions,strict=True);plain=request(adapter,body,definitions,strict=False)
            item=strict['tools'][0];other=plain['tools'][0]
            if identifier=='chat_completions':item=item['function'];other=other['function']
            self.assertTrue(item.pop('strict'))
            if identifier=='responses':self.assertIs(other.pop('strict'),False)
            else:self.assertNotIn('strict',other)
            self.assertEqual(item,other)

    def test_multiple_calls_incomplete_response_and_unknown_tool_are_not_actions(self):
        call={'type':'function_call','name':'submit_plan','arguments':'{}'}
        with self.assertRaises(PublicResponseStructureError):response(get('responses'),{'status':'completed','output':[call,call]})
        with self.assertRaises(ValueError):response(get('responses'),{'status':'incomplete','output':[call]})
        self.assertEqual(response(get('responses'),{'status':'completed','output':[call]}),('submit_plan',{}))

    def test_model_fallback_rechecks_lease_and_remembers_mode(self):
        from webapp.workflow_agents import model,StrictModeUnsupported,ModelServiceError
        lease=Mock();transport={'strict':True};protocol=Mock()
        with patch('webapp.workflow_agents._model_request',side_effect=[StrictModeUnsupported(400),{'action':'result'}]) as call:
            self.assertEqual(model(None,lease,{},None,'frozen',protocol=protocol,transport=transport),{'action':'result'})
            self.assertEqual([c.args[-1] for c in call.call_args_list],[True,False]);lease.check.assert_called_once()
        self.assertFalse(transport['strict'])
        with patch('webapp.workflow_agents._model_request',side_effect=ModelServiceError(502)) as call:
            with self.assertRaises(ModelServiceError):model(None,lease,{},None,'frozen',protocol=protocol)
            self.assertEqual(call.call_count,1)
