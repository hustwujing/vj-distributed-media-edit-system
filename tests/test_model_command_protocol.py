import copy,json,unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext
from types import SimpleNamespace
from jsonschema import ValidationError
from webapp import workflow_graph as graph
from webapp.workflow_model_protocol import (manager_schema,compile_manager,ToolProtocol,CommandError,
    schema_complexity,repair_options,required_delivery_extensions,required_delivery_names,scopes,targeted_user_inputs,
    normalize_planner_arguments)
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
            'dependencies':[],'trigger':{} if task is False else {'task_id':'render'},'input_batch':{'through_sequence':7,'inputs':[{'context':{'allowed_revision_tasks':['render']}}]} if user else None}


class ModelCommandTests(unittest.TestCase):
    def test_unique_trailing_underscore_acceptance_field_is_normalized(self):
        value={'tasks':[{'type':'agent','acceptance':[{'kind':'image','height_':64,'max_participation':.5}]}]}
        normalized,changes=normalize_planner_arguments('submit_typed_plan',value)
        self.assertEqual(normalized['tasks'][0]['acceptance'][0],{'kind':'image','height':64,'max_participation':.5})
        self.assertEqual(changes,['tasks.0.acceptance.0.height_->height'])

    def test_applied_user_change_adds_exact_delivery_names_but_not_inputs(self):
        value={'goal':'Edit source.mp4','authorization':{'delivery_targets':['video and cover']},
               'input_asset_names':['source.mp4'],
               'user_changes':[{'content':'Reuse source.mp4; deliver final.mp4 and cover.png.'}]}
        self.assertEqual(required_delivery_names(value),{'final.mp4','cover.png'})
        self.assertEqual(required_delivery_extensions(value),{'mp4','png'})

    def test_non_delivery_filename_is_not_required_after_user_change(self):
        value={'goal':'Deliver final.mp4','authorization':{'delivery_targets':['video']},
               'user_changes':[{'content':'Keep evidence.json internal only; deliver final.mp4.'}]}
        self.assertEqual(required_delivery_names(value),{'final.mp4'})

    def test_intermediate_reused_format_is_not_a_delivery_requirement(self):
        value={'goal':'另建 clip 节点处理成 MP4，保留该节点供后续复用。最终交付 final.png。',
               'authorization':{'delivery_targets':['128x64 PNG image']}}
        self.assertEqual(required_delivery_extensions(value),{'png'})

    def test_unique_requested_basename_is_host_compiled_for_agent_revision(self):
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Export','objective':'Export video',
              'outputs':['old.mp4','cover.png'],'output_roles':{'video':'old.mp4','cover':'cover.png'},
              'acceptance':[{'kind':'media','file':'old.mp4'},{'kind':'image','file':'cover.png'}],
              'basic_reason':'export','delivery_stage':True}
        value=frozen(task,user=True)
        value['input_batch']['inputs'][0]['content']='Deliver final.mp4 and cover.png.'
        command={'kind':'COMPLETE','reason':'Apply requested names','plan':{'changes':[{'task_id':'render','set':{
            'objective':'Export video and cover'}}]}}
        revised=compile_manager(command,value)['proposal']['revise_tasks'][0]
        self.assertEqual(revised['outputs'],['final.mp4','cover.png'])
        self.assertEqual(revised['output_roles'],{'video':'final.mp4','cover':'cover.png'})

    def test_targeted_task_input_cannot_request_a_successor_run(self):
        value=frozen(user=True);value['input_batch']['inputs'][0]['context']['targets']=['render']
        self.assertNotIn('NEW_RUN_REQUIRED',manager_schema(value)['properties']['kind']['enum'])
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertNotIn('new_run_required',protocol.schemas)
        self.assertNotIn('tasks',protocol.schemas['submit_typed_plan']['properties'])
        self.assertEqual(protocol.schemas['submit_typed_plan']['properties']['changes']['maxItems'],16)
        command={'kind':'NEW_RUN_REQUIRED','reason':'Move the targeted change','new_goal':'Different run'}
        with self.assertRaises(CommandError) as raised:compile_manager(command,value)
        self.assertEqual((raised.exception.code,raised.exception.path),('same_run_revision_required','kind'))

    def test_applied_user_change_keeps_its_authorized_revision_scope(self):
        value=frozen(user=True);record=value['input_batch']['inputs'][0]
        record['context']['targets']=['render'];value['input_batch']=None;value['user_changes']=[record];value['trigger']={}
        schema=manager_schema(value)
        changes=schema['properties']['plan']['anyOf'][0]['properties']['changes']
        self.assertEqual(changes['maxItems'],16)
        command={'kind':'COMPLETE','reason':'Apply retained input','plan':{'changes':[{
            'task_id':'render','set':{'objective':'Updated after the input drain'}}]}}
        revised=compile_manager(command,value)['proposal']['revise_tasks'][0]
        self.assertEqual(revised['objective'],'Updated after the input drain')

    def test_automatic_repair_trigger_owns_revision_count_over_input_history(self):
        value=frozen(user=True);record=value['input_batch']['inputs'][0]
        record['context']['targets']=['render'];value['input_batch']=None;value['user_changes']=[record]
        value['trigger']={'review_rejected':True}
        self.assertEqual(scopes(value),('',None))
        self.assertFalse(targeted_user_inputs(value))

    def test_explicit_output_extensions_promote_matching_files_to_delivery(self):
        f=frozen(False);f['goal']='Deliver the edited PNG with original JSON and TXT records'
        task={'id':'records','type':'agent','profile':'editing_implementer','name':'Records','objective':'Write records',
              'files':[{'path':'ocr.json','roles':['json']},{'path':'ocr.txt','roles':['text']}],
              'acceptance':[{'kind':'json','file':'ocr.json','stage':'current','source':'technical'},
                            {'kind':'text','file':'ocr.txt','contains':['text'],'stage':'current','source':'user'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Deliver records','plan':{'new_tasks':[task]}},f)
        compiled=result['proposal']['add_tasks'][0]
        self.assertTrue(compiled['delivery_stage'])
        finals={(check['file'],check['source'],check['stage']) for check in compiled['acceptance'] if check['stage']=='final'}
        self.assertEqual(finals,{('ocr.json','user','final'),('ocr.txt','user','final')})

    def test_explicit_png_does_not_promote_an_operation_preview(self):
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='image.ocr')
        f=frozen(False);f['goal']='Deliver the final PNG with original JSON record';f['operations']=[operation]
        task={'id':'ocr','type':'operation','name':'OCR','objective':'Read text','operation':'image.ocr',
              'inputs':{'source':{'asset':'asset'}},'parameters':{},'acceptance':[]}
        result=compile_manager({'kind':'APPLY_PLAN','reason':'Read text','plan':{'new_tasks':[task]}},f)
        compiled=result['proposal']['add_tasks'][0]
        finals={(check['file'],check['source']) for check in compiled['acceptance'] if check['stage']=='final'}
        self.assertEqual(finals,{('ocr.json','user')})

    def test_explicit_report_request_promotes_the_registered_report_role(self):
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='audio.process')
        f=frozen(False);f['goal']='Deliver a WAV and the objective audio-process report';f['operations']=[operation]
        task={'id':'process','type':'operation','name':'Process','objective':'Process audio','operation':'audio.process',
              'inputs':{'source':{'asset':'asset'}},'parameters':{},'acceptance':[{'kind':'media','stage':'final'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Deliver audio and report','plan':{'new_tasks':[task]}},f)
        compiled=result['proposal']['add_tasks'][0]
        finals={(check['file'],check['source']) for check in compiled['acceptance'] if check['stage']=='final'}
        self.assertIn(('audio-process-report.json','user'),finals)

    def test_unique_equivalent_delivery_task_replaces_the_old_leaf(self):
        old={'id':'old','type':'agent','profile':'editing_implementer','name':'Old','objective':'Old attempt',
             'outputs':['old.png'],'output_roles':{'image':'old.png'},'delivery_stage':True,
             'acceptance':[{'kind':'image','file':'old.png','width':1024,'height':768,'source':'user','stage':'final'}],
             'basic_reason':'old'}
        f=frozen(old);f['graph_version']=2;f['trigger']={};f['tasks'][0].update(id='old',status='succeeded')
        new={'id':'fixed','type':'agent','profile':'editing_implementer','name':'Fixed','objective':'Fixed attempt',
             'files':[{'path':'fixed.png','roles':['image']}],
             'acceptance':[{'kind':'image','width':1024,'height':768,'stage':'final','source':'user'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Use the fixed candidate','plan':{'new_tasks':[new]}},f)
        self.assertEqual(result['proposal']['replacements'],{'old':'fixed'})

    def test_ambiguous_equivalent_delivery_tasks_are_not_guessed_as_replacements(self):
        old={'id':'old','type':'agent','profile':'editing_implementer','name':'Old','objective':'Old attempt',
             'outputs':['old.png'],'output_roles':{'image':'old.png'},'delivery_stage':True,
             'acceptance':[{'kind':'image','file':'old.png','width':1024,'height':768,'source':'user','stage':'final'}],
             'basic_reason':'old'}
        f=frozen(old);f['graph_version']=2;f['trigger']={};f['tasks'][0]['id']='old';second=json.loads(json.dumps(f['tasks'][0]));second['id']='other';second['spec']['id']='other';f['tasks'].append(second)
        new={'id':'fixed','type':'agent','profile':'editing_implementer','name':'Fixed','objective':'Fixed attempt',
             'files':[{'path':'fixed.png','roles':['image']}],
             'acceptance':[{'kind':'image','width':1024,'height':768,'stage':'final','source':'user'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Keep distinct variants','plan':{'new_tasks':[new]}},f)
        self.assertEqual(result['proposal']['replacements'],{})

    def test_inconclusive_review_recommends_evidence_before_rerender(self):
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Render','objective':'Render video',
              'outputs':['final.mp4'],'output_roles':{'video':'final.mp4'},'acceptance':[{'kind':'media'}],
              'basic_reason':'render','delivery_stage':True}
        f=frozen(task);f['tasks'][0].update(status='succeeded',candidate_result=['final.mp4'],
            quality={'technical_valid':True,'passed':True})
        f['review_feedback']=[{'subject_task':'render','decision':{'verdict':'inconclusive',
            'feedback':'Need RMS evidence for the final audio fade and music mix.'}}]
        option=next(item for item in repair_options(f) if item['action']=='collect_evidence_then_review_again')
        self.assertEqual(option['task_id'],'render')
        self.assertEqual(option['suggested_modes'],['audio_envelope','audio_compare'])
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='media.evidence')
        f['operations']=[operation]
        protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},
            contracts={operation['id']:operation},draft={'status':'OPEN'})
        task_schema=protocol.schemas['submit_typed_plan']['properties']['tasks']
        self.assertEqual(task_schema['minItems'],1)
        self.assertNotIn('AgentCommandTask',protocol.schemas['submit_typed_plan'].get('$defs',{}))
        invalid={'kind':'APPLY_PLAN','reason':'Add another business Agent','plan':{'new_tasks':[{
            'id':'copy','type':'agent','profile':'editing_implementer','name':'Copy','objective':'Copy media',
            'files':[{'path':'copy.png','roles':['image']}],'acceptance':[{'kind':'image'}]}]}}
        with self.assertRaises(CommandError) as raised:compile_manager(invalid,f)
        self.assertEqual(raised.exception.code,'evidence_task_required')

    def test_empty_apply_plan_seals_only_a_completed_delivery_graph(self):
        completed=frozen();completed['tasks'][0]['status']='succeeded'
        result=compile_manager({'kind':'APPLY_PLAN','reason':'All work is complete','plan':{}},completed)
        self.assertEqual(result['kind'],'COMPLETE')
        self.assertTrue(result['proposal']['sealed'])
        self.assertEqual(result['proposal']['replan_after'],[])

        for value in (frozen(),frozen({**completed['tasks'][0]['spec'],'delivery_stage':False})):
            value['tasks'][0]['status']='paused' if value['tasks'][0]['spec'].get('delivery_stage') else 'succeeded'
            with self.assertRaises(CommandError) as raised:
                compile_manager({'kind':'APPLY_PLAN','reason':'No executable changes','plan':{}},value)
            self.assertEqual(raised.exception.code,'missing_wait_work')

    def test_empty_apply_plan_does_not_hide_dependency_changes(self):
        completed=frozen();completed['tasks'][0]['status']='succeeded'
        with self.assertRaises(CommandError) as raised:
            compile_manager({'kind':'APPLY_PLAN','reason':'Remove an edge','plan':{
                'remove_dependencies':[{'upstream':'render','downstream':'other'}]}},completed)
        self.assertEqual(raised.exception.code,'missing_wait_work')

    def test_evidence_cannot_wait_on_unaccepted_output_from_paused_task(self):
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Render','objective':'Render audio',
              'outputs':['final.wav'],'output_roles':{'audio':'final.wav'},'acceptance':[{'kind':'media'}],
              'basic_reason':'render','delivery_stage':True}
        f=frozen(task);f['tasks'][0].update(status='paused',candidate_result=['final.wav'],
            quality={'technical_valid':True,'passed':True})
        f['review_feedback']=[{'subject_task':'render','decision':{'verdict':'inconclusive',
            'feedback':'Need bounded audio evidence.'}}]
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='media.evidence')
        f['operations']=[operation]
        evidence={'id':'measure','type':'operation','name':'Measure','objective':'Measure candidate',
                  'operation':'media.evidence','purpose':'evidence','parameters':{'mode':'audio'},
                  'inputs':{'source':{'step':'render','role':'audio'}}}
        with self.assertRaises(CommandError) as raised:
            compile_manager({'kind':'APPLY_PLAN','reason':'Collect evidence','plan':{'new_tasks':[evidence]}},f)
        self.assertEqual(raised.exception.code,'evidence_candidate_required')
        self.assertEqual(raised.exception.path,'plan.new_tasks.0.inputs.source')

    def test_changes_preserve_frozen_contract_and_bind_original_request(self):
        f=frozen();command={'kind':'COMPLETE','reason':'Repair script','plan':{'changes':[{'task_id':'render','set':{'argv':['python','-c','print(1)']}}]}}
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
        cmd={'kind':'COMPLETE','reason':'User feedback','plan':{'changes':[{'task_id':'render','set':{'objective':'Updated goal'}}]}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)
        self.assertEqual(result['input_through_sequence'],7)
        self.assertEqual(result['proposal']['revise_tasks'][0]['objective'],'Updated goal')
        cmd['plan']['changes'][0]['set']['output_roles']={'wrong':'report.txt'}
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_automatic_replacement_inherits_exact_requirement_without_model_retyping(self):
        f=frozen();schema=manager_schema(f)
        self.assertEqual(schema['properties']['plan']['anyOf'][0]['properties']['new_tasks']['maxItems'],0)
        implementation={'name':'New implementation','type':'script','argv':['python','-c','print(1)'],
                        'files':[{'path':'report.txt','roles':['report']}],'basic_reason':'Local repair'}
        cmd={'kind':'APPLY_PLAN','reason':'Replace failed implementation','plan':{
            'repair_replacements':[{'task_id':'render','implementation':implementation}]}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)['proposal']
        replacement=result['replacements']['render'];self.assertTrue(replacement.startswith('render_r_'))
        self.assertEqual(result['add_tasks'][0]['id'],replacement)
        self.assertEqual(result['replan_after'],[replacement])
        for key in ('objective','acceptance','semantics','delivery_stage'):
            self.assertEqual(result['add_tasks'][0][key],f['tasks'][0]['spec'][key])
        implementation['acceptance']=[]
        with self.assertRaises(ValidationError):transform(cmd,schema,encode=True)
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_operation_replacement_does_not_reclassify_inherited_technical_acceptance(self):
        from webapp.operations import catalog
        op=next(item for item in catalog()['operations'] if item['id']=='image.perspective')
        spec={'id':'render','type':'operation','name':'render','objective':'Preserve the image requirement',
              'operation':'image.perspective','implementation':op['implementation'],
              'inputs':{'source':{'asset':'asset'}},'parameters':{'points':[[0,0],[1,0],[1,1],[0,1]]},
              'acceptance':[{'kind':'image','file':'*','width':64,'height':64,'source':'user'},
                            {'kind':'image','file':'*','source':'technical'}],
              'semantics':{'policy_version':'media-semantics-v1'},'delivery_stage':False}
        f=frozen(spec);f['operations']=[{**op,'authorized':True}]
        implementation={'name':'Replacement operation','type':'operation','operation':'image.perspective',
                        'inputs':{'source':{'asset':'asset'}},
                        'parameters':{'points':[[0,0],[1,0],[1,1],[0,1]]},'basic_reason':'Bounded repair'}
        cmd={'kind':'APPLY_PLAN','reason':'Replace failed operation','plan':{
            'repair_replacements':[{'task_id':'render','implementation':implementation}]}}
        schema=manager_schema(f,{'image.perspective':op});result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)['proposal']
        self.assertEqual(result['add_tasks'][0]['acceptance'],f['tasks'][0]['spec']['acceptance'])

    def test_output_declaration_and_completion_are_host_derived(self):
        f=frozen(False);schema=manager_schema(f)
        task={'id':'render','type':'script','name':'render','objective':'Write','argv':['python','-c','pass'],
              'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}],'basic_reason':'local'}
        cmd={'kind':'COMPLETE','reason':'Done planning','plan':{'new_tasks':[task]}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)
        self.assertEqual(result['proposal']['add_tasks'][0]['outputs'],['report.txt']);self.assertEqual(result['proposal']['replan_after'],[])
        self.assertTrue(result['proposal']['add_tasks'][0]['delivery_stage'])
        task['delivery_stage']=True
        with self.assertRaises(ValidationError):transform(cmd,schema,encode=True)
        task.pop('delivery_stage')
        cmd['plan']['continuation']={'mode':'complete'}
        with self.assertRaises(ValidationError):transform(cmd,schema,encode=True)
        cmd['plan'].pop('continuation');task['files'].append({'path':'other.txt','roles':['report']})
        with self.assertRaises(CommandError):compile_manager(cmd,f)

    def test_operation_acceptance_is_bound_to_the_semantic_output_role(self):
        from webapp.operations import catalog
        for operation in ('image.remove_background','image.refine_zim'):
            with self.subTest(operation=operation):
                op=next(item for item in catalog()['operations'] if item['id']==operation)
                f=frozen(False);f['operations']=[{**op,'authorized':True}]
                task={'id':'cutout','type':'operation','name':'Cutout','objective':'Remove background',
                      'operation':operation,'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],
                      'parameters':{},'acceptance':[{'kind':'image','transparent_subject':True,'stage':'final'}]}
                command={'kind':'COMPLETE','reason':'Deliver cutout','plan':{'new_tasks':[task]}}
                result=compile_manager(command,f)['proposal']['add_tasks'][0]
                user=[check for check in result['acceptance'] if check['source']=='user'][0]
                self.assertEqual(user['file'],'foreground.png')
                technical={check['file'] for check in result['acceptance'] if check['source']=='technical'}
                self.assertTrue(technical)

    def test_model_cannot_claim_host_technical_acceptance_or_bypass_output_binding(self):
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='image.remove_background')
        f=frozen(False);f['operations']=[{**operation,'authorized':True}]
        task={'id':'cutout','type':'operation','name':'Cutout','objective':'Remove only the outer background',
              'operation':'image.remove_background','inputs':{'source':{'asset':'asset'}},'parameters':{},
              'acceptance':[{'kind':'image','source':'technical','transparent_subject':True,
                             'has_alpha':True,'min_transparent_fraction':.05}]}
        command={'kind':'APPLY_PLAN','reason':'Prepare the foreground','plan':{'new_tasks':[task]}}
        result=compile_manager(command,f)['proposal']['add_tasks'][0]
        claimed=[check for check in result['acceptance'] if check.get('transparent_subject') is True]
        self.assertEqual(len(claimed),1)
        self.assertEqual((claimed[0]['source'],claimed[0]['file']),('user','foreground.png'))
        technical={check['file'] for check in result['acceptance'] if check['source']=='technical'}
        self.assertEqual(technical,{'foreground.png','mask.png'})
        schema=manager_schema(f)
        with self.assertRaises(ValidationError):transform(command,schema,encode=True)

    def test_host_derives_input_dependencies_and_waits_for_batch_leaves(self):
        f=frozen(False);schema=manager_schema(f)
        first={'id':'prepare','type':'agent','profile':'editing_implementer','name':'Prepare','objective':'Prepare image',
               'files':[{'path':'prepared.png','roles':['primary_image']}],'acceptance':[{'kind':'image'}]}
        final={'id':'deliver','type':'agent','profile':'editing_implementer','name':'Deliver','objective':'Deliver image',
               'input_bindings':[{'role':'source','ref':{'step':'prepare','role':'primary_image'}}],
               'files':[{'path':'final.png','roles':['primary_image']}],
               'acceptance':[{'kind':'image','stage':'final'}]}
        cmd={'kind':'APPLY_PLAN','reason':'Typed batch','plan':{'new_tasks':[first,final]}}
        result=compile_manager(transform(transform(cmd,schema,encode=True),schema),f)['proposal']
        self.assertEqual(result['add_dependencies'],[{'upstream':'prepare','downstream':'deliver'}])
        self.assertEqual(result['replan_after'],['deliver'])

    def test_agent_input_bindings_compile_to_one_authoritative_map(self):
        f=frozen(False);schema=manager_schema(f)
        task={'id':'deliver','type':'agent','profile':'editing_implementer','name':'Deliver','objective':'Deliver image',
              'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],
              'files':[{'path':'final.png','roles':['primary_image']}],'acceptance':[{'kind':'image','stage':'final'}]}
        command={'kind':'COMPLETE','reason':'Deliver','plan':{'new_tasks':[task]}}
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
            'kind':'APPLY_PLAN','reason':'Inspect'}
        result=p.decode('submit_typed_plan',arguments)
        self.assertEqual(result['command']['tasks'][0]['inputs']['source']['asset'],'asset')
        self.assertEqual(result['command']['tasks'][0]['acceptance'][0]['file'],'invented.json')
        self.assertNotIn('continuation',result['command'])
        self.assertEqual(set(result['_normalizations']),{'tasks.0.operation_inputs_mapped'})

    def test_planner_normalizes_common_flat_operation_input_bindings(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.concat_sequence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op},draft={'status':'OPEN'})
        base={'id':'concat','type':'operation','name':'Concat','objective':'Join clips','operation':'media.concat_sequence',
              'parameters':{'roles':['clip_1'],'audio_mode':'preserve','threads':2}}
        for inputs in ([{'name':'clip_1','asset':'asset'}],[{'clip_1':{'asset':'asset'}}]):
            with self.subTest(inputs=inputs):
                arguments={'tasks':[{**base,'inputs':inputs}],'kind':'APPLY_PLAN','reason':'Join'}
                result=p.decode('submit_typed_plan',arguments)
                self.assertEqual(result['command']['tasks'][0]['input_bindings'],[{'role':'clip_1','ref':{'asset':'asset'}}])

    def test_noncut_transition_default_matches_runtime_contract(self):
        from webapp.operation_parameters import EditTransition
        self.assertEqual(EditTransition(type='cut').duration,0)
        self.assertEqual(EditTransition(type='fade').duration,.4)
        self.assertEqual(EditTransition(type='pixel_dissolve').duration,.4)
        schema=EditTransition.model_json_schema()
        self.assertIn('linear crossfade',schema['properties']['type']['description'])
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):EditTransition(type='dissolve')
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
              'inputs':{'source':{'asset':'asset'}},'acceptance':[{'kind':'image','stage':'final'}]}
        command={'kind':'COMPLETE','reason':'Adjust','plan':{'new_tasks':[task]}}
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
            'files':[{'path':'report.txt','roles':['report']}],'acceptance':[{'kind':'file','stage':'final'}],'basic_reason':'local'}]}}
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
        self.assertNotIn('tasks',protocol.schemas['submit_typed_plan']['properties'])

    def test_loaded_operation_contract_exposes_required_roles_and_parameters(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.evidence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        command={'kind':'APPLY_PLAN','reason':'Collect audio evidence','plan':{'new_tasks':[{
            'id':'evidence','type':'operation','name':'Evidence','objective':'Inspect authorized audio',
            'operation':'media.evidence','purpose':'evidence','parameters':{'mode':'audio'},'inputs':{'source':{'asset':'asset'}}}]}}
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
               'input_bindings':[{'role':'clip_1','ref':{'asset':'asset'}}],
               'acceptance':[{'kind':'media','stage':'final'}]}
        wire=transform({'kind':'COMPLETE','reason':'Join clips','plan':{'new_tasks':[draft]}},p.schemas['submit_plan'],encode=True)
        task=p.decode('submit_plan',wire)['proposal']['proposal']['add_tasks'][0]
        self.assertEqual([item['kind'] for item in task['acceptance']],['media','media','json'])
        self.assertEqual([item['source'] for item in task['acceptance']],['user','technical','technical'])
        self.assertTrue(task['acceptance'][1]['decode'])
        self.assertIn('registered operation',task['basic_reason'])

    def test_operation_business_acceptance_is_preserved_without_model_owned_filename(self):
        from webapp.operations import catalog
        op=next(x for x in catalog()['operations'] if x['id']=='media.concat_sequence')
        f=frozen(False);f['operations']=[op]
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},contracts={op['id']:op})
        draft={'id':'concat','type':'operation','name':'Join','objective':'Join clips','operation':'media.concat_sequence',
               'parameters':{'roles':['clip_1'],'audio_mode':'preserve','threads':2},
               'input_bindings':[{'role':'clip_1','ref':{'asset':'asset'}}],
               'acceptance':[{'kind':'media','file':'guessed.mp4','width':640,'height':360,'min_duration':11.8,'max_duration':12.2,'video':True,'stage':'final'}]}
        command={'kind':'COMPLETE','reason':'Join','plan':{'new_tasks':[draft]}}
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
              'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],
              'acceptance':[{'kind':'media','stage':'final'}]}
        command={'kind':'COMPLETE','reason':'Edit','plan':{'new_tasks':[base]}}
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
            'acceptance':[{'kind':'image','file':'final.png'},{'kind':'image','file':'final.jpg'}]}}]}}
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

    def test_input_asset_filename_is_not_a_required_delivery_format(self):
        from webapp.workflow_model_protocol import required_delivery_extensions
        frozen_review={'goal':'Use music.wav under the video and deliver MP4',
                       'authorization':{'delivery_targets':['complete video and requested attachments']},
                       'input_asset_names':['music.wav']}
        self.assertEqual(required_delivery_extensions(frozen_review),{'mp4'})
        frozen_review['goal']='Use music.wav under the video and deliver MP4 plus WAV'
        # The repeated extension outside the exact input filename remains an explicit output request.
        self.assertEqual(required_delivery_extensions(frozen_review),{'mp4','wav'})

    def test_explicit_non_delivery_filename_does_not_create_a_format_requirement(self):
        from webapp.workflow_model_protocol import required_delivery_extensions
        frozen={'goal':'保存 reference.png，声明为非交付；最终交付 5 秒 MP4',
                'authorization':{'delivery_targets':['符合需求的视频及要求的附加文件']}}
        self.assertEqual(required_delivery_extensions(frozen),{'mp4'})
        frozen['goal']='reference.png 仅作为中间参考；最终交付 MP4 和 PNG 海报'
        self.assertEqual(required_delivery_extensions(frozen),{'mp4','png'})

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
