import copy,json,unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext
from types import SimpleNamespace
from jsonschema import ValidationError
from webapp import workflow_graph as graph
from webapp.workflow_model_protocol import (manager_schema,compile_manager,ToolProtocol,CommandError,
    schema_complexity,repair_options,required_delivery_extensions,required_delivery_names,scopes,targeted_user_inputs,
    normalize_planner_arguments)
from webapp.strict_schema import obj,schema_for,transform,validation_feedback
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


def delivery(*,filenames=(),formats=(),roles=(),excluded=()):
    return {'delivery_contract':{'filenames':list(filenames),'formats':list(formats),
                                 'roles':list(roles),'excluded_filenames':list(excluded)}}


class ModelCommandTests(unittest.TestCase):
    def test_exhausted_repair_is_not_offered_as_revision_or_replacement(self):
        from webapp.workflow_model_protocol import available_changes,automatic_replacement_tasks
        for count,remaining in ((1,1),(2,0),(3,0)):
            with self.subTest(repair_count=count):
                value=frozen();value['tasks'][0].update(repair_count=count,user_revision_count=0)
                self.assertEqual(bool(available_changes(value)),bool(remaining))
                self.assertEqual(bool(automatic_replacement_tasks(value)),bool(remaining))
                schema=manager_schema(value)['properties']['plan']['anyOf'][0]['properties']
                self.assertEqual(bool(schema['changes']['maxItems']),bool(remaining))
                self.assertEqual(bool(schema['repair_replacements']['maxItems']),bool(remaining))
                protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
                self.assertEqual(protocol.context()['model_command_contract']['task_repair_budgets']['render'],
                    {'automatic_remaining':remaining,'user_revision_remaining':5})
                if remaining:continue
                self.assertFalse(repair_options(value))
                with self.assertRaisesRegex(CommandError,'outside.*scope'):
                    compile_manager({'kind':'APPLY_PLAN','reason':'Retry','plan':{
                        'repair_replacements':[{'task_id':'render','implementation':{}}]}},value)
                with self.assertRaises(CommandError):
                    compile_manager({'kind':'APPLY_PLAN','reason':'Retry','plan':{
                        'changes':[{'task_id':'render','set':{}}]}},value)

    def test_user_revision_budget_is_independent_of_automatic_repairs(self):
        from webapp.workflow_model_protocol import available_changes
        value=frozen(user=True);value['tasks'][0].update(repair_count=2,user_revision_count=4)
        self.assertIn('render',available_changes(value))
        value['tasks'][0]['user_revision_count']=5
        self.assertNotIn('render',available_changes(value))
        self.assertEqual(manager_schema(value)['properties']['plan']['anyOf'][0]['properties']['changes']['maxItems'],0)

    def test_completion_guidance_does_not_treat_appending_as_replacement(self):
        value=frozen();value['trigger']={'completed':['render']};value['tasks'][0]['status']='succeeded'
        value['tasks'][0].update(repair_count=2,user_revision_count=0)
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertIn('complete_run',protocol.schemas)
        self.assertIn('submit_typed_plan',protocol.schemas)
        self.assertIn('all earlier delivery Tasks remain mandatory',protocol.context()['model_command_contract']['planning'])

    def test_non_idempotent_repair_offers_replacement_not_illegal_in_place_revision(self):
        from webapp.operations import get as operation
        from webapp.workflow_model_protocol import available_changes
        for name,params in [('image.edit_region',{'box':[1,1,10,10],'prompt':'Make the subject blue'}),
                            ('image.decompose',{'layer_count':3,'prompt':'Preserve the composition'})]:
            with self.subTest(operation=name):
                op=operation(name).public()
                spec={'id':'render','type':'operation','name':'Edit','objective':'Preserve required model and content',
                    'operation':name,'implementation':op['implementation'],'inputs':{'source':{'asset':'asset'}},
                    'parameters':params,'acceptance':[{'kind':'image','width':64,'stage':'final'}],'delivery_stage':True}
                f=frozen(spec);f['operations']=[{**op,'authorized':True}]
                self.assertNotIn('render',available_changes(f))
                option=next(x for x in repair_options(f) if x['task_id']=='render')
                self.assertEqual(option['action'],'repair_replacement')
                self.assertEqual(option['plan_field'],'repair_replacements')
                protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},
                    contracts={name:op},draft={'status':'OPEN'})
                self.assertEqual(protocol.schemas['submit_typed_plan']['properties']['changes']['maxItems'],0)
                self.assertIn(option,protocol.context()['model_command_contract']['repair_options'])
                bad={'kind':'APPLY_PLAN','reason':'Repair','plan':{'changes':[{'task_id':'render','set':{'parameters':params}}]}}
                with self.assertRaises(CommandError):compile_manager(bad,f)
                implementation={'type':'operation','name':'Repair via new attempt','operation':name,
                    'inputs':{'source':{'asset':'asset'}},'parameters':params}
                command={'kind':'APPLY_PLAN','reason':'Repair within authorization','plan':{
                    'repair_replacements':[{'task_id':'render','implementation':implementation}]}}
                schema=manager_schema(f,{name:op})
                result=compile_manager(transform(transform(command,schema,encode=True),schema),f)['proposal']
                self.assertNotEqual(result['replacements']['render'],'render')
                self.assertFalse(result['revise_tasks'])
                for field in ('objective','acceptance','semantics','delivery_stage'):
                    self.assertEqual(result['add_tasks'][0][field],f['tasks'][0]['spec'][field])
                user=frozen(spec,user=True)
                self.assertIn('render',available_changes(user),'Explicit user revision retains its separately validated authority')
        local=copy.deepcopy(spec);local.update(operation='image.segment',implementation='fixed_image@1',parameters={'box':[1,1,10,10]})
        self.assertIn('render',available_changes(frozen(local)))

    def test_delivery_format_binding_preserves_type_and_multi_file_wildcards(self):
        from webapp.workflow_model_protocol import bind_explicit_delivery_formats
        raw={'acceptance':[{'kind':'media','video':True,'width':854,'file':'*','source':'user','stage':'current'},
            {'kind':'subtitle','file':'*','source':'user','stage':'current'},
            {'kind':'json','file':'*','source':'user','stage':'current'}]}
        bind_explicit_delivery_formats(raw,['final.mp4','final.srt','report.json'],delivery(formats=('mp4','srt','json'),roles=('report',)))
        finals={x['file']:x['kind'] for x in raw['acceptance'] if x['stage']=='final'}
        self.assertEqual(finals,{'final.mp4':'media','final.srt':'subtitle','report.json':'json'})
        self.assertEqual(len([x for x in raw['acceptance'] if x['file']=='*']),3)
        raw={'acceptance':[{'kind':'image','width':64,'file':'*','source':'user','stage':'current'}]}
        bind_explicit_delivery_formats(raw,['a.png','b.png'],delivery(formats=('png',)))
        self.assertEqual({x['file'] for x in raw['acceptance'] if x['stage']=='final'},{'a.png','b.png'})

    def test_operation_srt_delivery_never_retargets_video_acceptance(self):
        from webapp.operations import get as operation
        value=frozen(False);value.update(goal='Deliver MP4 and SRT',operations=[operation('media.edit').public()])
        task={'id':'render','name':'Render','type':'operation','operation':'media.edit','objective':'Video and subtitles',
            'inputs':{'source':{'asset':'clip'},'transcript':{'asset':'words'}},
            'parameters':{'edit_plan':{'clips':[{'asset':'source','end':1}],'subtitles':{'from_transcripts':True}}},
            'acceptance':[{'kind':'media','video':True,'width':854,'stage':'final'},{'kind':'subtitle','stage':'final'}]}
        compiled=compile_manager({'kind':'COMPLETE','reason':'Deliver both',**delivery(formats=('mp4','srt')),
                                  'plan':{'new_tasks':[task]}},value)['proposal']['add_tasks'][0]
        self.assertEqual(compiled['on_failure'],'diagnose')
        self.assertTrue(any(check['kind']=='subtitle' and check['file']=='final.srt' for check in compiled['acceptance']))
        self.assertTrue(all(check['file']!='final.srt' for check in compiled['acceptance'] if check['kind']=='media'))

    def test_read_only_response_tool_is_bound_to_an_empty_run(self):
        f=frozen(False);f.update(can_respond=True,scope_confirmation_required=True)
        protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertIn('respond',protocol.schemas)
        result=protocol.decode('respond',transform({'reason':'上一轮成果已发布，本次没有重新处理。'},protocol.schemas['respond'],encode=True))
        self.assertEqual(result['proposal']['kind'],'RESPOND');self.assertIsNone(result['proposal']['proposal'])
        f['can_respond']=False
        self.assertNotIn('respond',ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'}).schemas)

    def test_native_business_agent_schema_rejects_control_profiles_before_compilation(self):
        protocol=ToolProtocol(profile('media_planner'),frozen(False),{'control_kind':'PLAN'},draft={'status':'OPEN'})
        task={'id':'work','name':'Work','type':'agent','profile':'editing_implementer','objective':'Create output',
              'files':[{'path':'result.txt','roles':['report']}],
              'acceptance':[{'kind':'text','contains':['result'],'stage':'final'}]}
        payload={'kind':'APPLY_PLAN','reason':'Authorized work','tasks':[task]}
        schema=protocol.schemas['submit_typed_plan']
        self.assertEqual(protocol.decode('submit_typed_plan',transform(payload,schema,encode=True))['action'],'draft_batch_commit')
        for identifier in ('media_planner','visual_quality_reviewer','delivery_reviewer','media_failure_diagnostician'):
            with self.subTest(profile=identifier):
                bad=copy.deepcopy(payload);bad['tasks'][0]['profile']=identifier
                with self.assertRaises(ValidationError):protocol.decode('submit_typed_plan',transform(bad,schema,encode=True))
    def test_requested_quality_report_gets_canonical_role_without_losing_custom_role(self):
        f=frozen(False);f['goal']='Deliver a video and a quality report'
        task={'id':'final','name':'Final output','type':'agent','profile':'editing_implementer',
              'objective':'Produce the requested video and report','files':[
                  {'path':'final.mp4','roles':['final_video']},{'path':'quality.json','roles':['quality_report']}],
              'acceptance':[{'kind':'media','video':True,'stage':'final'},{'kind':'json','required_keys':['duration'],'stage':'final'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Deliver authorized outputs',
            **delivery(filenames=('final.mp4','quality.json'),roles=('report',)),'plan':{'new_tasks':[task]}},f)['proposal']
        roles=result['add_tasks'][0]['output_roles']
        self.assertEqual(roles,{'final_video':'final.mp4','quality_report':'quality.json','report':'quality.json'})
        duplicate=copy.deepcopy(task);duplicate['files'].append({'path':'other.json','roles':['other_report']})
        with self.assertRaisesRegex(CommandError,'multiple report roles'):
            compile_manager({'kind':'COMPLETE','reason':'Ambiguous reports',**delivery(roles=('report',)),
                             'plan':{'new_tasks':[duplicate]}},f)
        duplicate['files'][1]['roles'].append('report')
        self.assertEqual(compile_manager({'kind':'COMPLETE','reason':'Explicit role',**delivery(roles=('report',)),
            'plan':{'new_tasks':[duplicate]}},f)['proposal']['add_tasks'][0]['output_roles']['report'],'quality.json')

    def test_tool_envelope_errors_are_specific_and_do_not_echo_response_content(self):
        cases=[('anthropic',{'stop_reason':'end_turn','content':[{'type':'text','text':'private response'}]},'tool_call_required'),
               ('anthropic',{'stop_reason':'tool_use','content':[]},'ambiguous_tool_calls'),
               ('anthropic',{'stop_reason':'tool_use','content':[{'type':'tool_use','name':'inspect','input':[]}]},'tool_arguments_not_object'),
               ('anthropic',{'stop_reason':'tool_use','content':[{'type':'tool_use','input':{}}]},'invalid_tool_name'),
               ('responses',{'status':'completed','output':['private response']},'invalid_tool_items'),
               ('chat_completions',{'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'type':'function','function':None}]}}]},'invalid_tool_envelope')]
        for protocol,payload,code in cases:
            with self.subTest(protocol=protocol,code=code):
                with self.assertRaises(PublicResponseStructureError) as caught:response(get(protocol),payload)
                self.assertEqual(caught.exception.code,code)
                self.assertNotIn('private response',str(caught.exception))

    def test_completed_delivery_batch_keeps_planning_and_optional_completion(self):
        value=frozen();value['tasks'][0]['status']='succeeded'
        value['trigger']={'completed':['render']};value['input_batch']=None
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertIn('complete_run',protocol.schemas)
        self.assertIn('submit_typed_plan',protocol.schemas)
        result=protocol.decode('complete_run',{'reason':'All declared work completed'})['proposal']
        self.assertEqual(result['kind'],'COMPLETE')
        self.assertTrue(result['proposal']['sealed'])
        self.assertEqual(result['proposal']['add_tasks'],[])

    def test_delivered_ocr_report_cannot_remove_the_tool_to_plan_remaining_image_edit(self):
        value=frozen();value['goal']='Deliver the OCR report and an edited image with the requested text replacement.'
        value['tasks'][0]['status']='succeeded';value['tasks'][0]['spec']['name']='OCR report'
        value['trigger']={'completed':['render']};value['input_batch']=None
        task={'id':'edit_image','type':'agent','profile':'editing_implementer','name':'Replace text',
              'objective':'Perform the still outstanding authorized image edit',
              'files':[{'path':'edited.png','roles':['primary_image']}],
              'acceptance':[{'kind':'image','stage':'final'}]}
        for finalize in (False,True):
            with self.subTest(finalize=finalize):
                protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'},finalize=finalize)
                wire=transform({'tasks':[task],'kind':'APPLY_PLAN','reason':'The OCR batch is done; the requested image edit remains'},protocol.schemas['submit_typed_plan'],encode=True)
                result=protocol.decode('submit_typed_plan',wire)
                self.assertEqual(result['action'],'draft_batch_commit')
                self.assertIn('does not prove the entire user goal is complete',protocol.context()['model_command_contract']['planning'])

    def test_incomplete_or_non_delivery_graph_keeps_planning_tools(self):
        for status,delivery,trigger in [('paused',True,{'completed':['render']}),
                                        ('succeeded',False,{'completed':['render']}),
                                        ('succeeded',True,{'review_incomplete':'review'})]:
            with self.subTest(status=status,delivery=delivery,trigger=trigger):
                value=frozen();value['tasks'][0]['status']=status
                value['tasks'][0]['spec']['delivery_stage']=delivery
                value['trigger']=trigger;value['input_batch']=None
                protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
                self.assertIn('submit_typed_plan',protocol.schemas)
                self.assertNotIn('complete_run',protocol.schemas)

    def test_new_final_review_evidence_allows_explicit_resubmission_without_empty_work(self):
        value=frozen();value['tasks'][0]['status']='succeeded';value['input_batch']=None
        value['tasks'][0]['accepted_result']=['report.txt']
        value['trigger']={'repair_resume':'review_evidence','task_id':''}
        value['repair_context']={'active':True,'source':'review_evidence','subject_task':'','opened_graph_version':2}
        value['review_feedback']=[{'purpose':'final','subject_task':'','decision':{
            'verdict':'inconclusive','feedback':'Need independent final delivery evidence'}}]
        evidence=copy.deepcopy(value['tasks'][0]);evidence['id']='evidence';evidence['created_graph_version']=3
        evidence['spec'].update(id='evidence',purpose='evidence',delivery_stage=False)
        value['tasks'].append(evidence)
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertIn('complete_run',protocol.schemas)
        self.assertNotIn('submit_typed_plan',protocol.schemas)
        result=protocol.decode('complete_run',{'reason':'New evidence is accepted; request independent final review'})['proposal']
        self.assertEqual(result['kind'],'COMPLETE');self.assertTrue(result['proposal']['sealed'])
        with self.assertRaises(CommandError) as error:
            compile_manager({'kind':'APPLY_PLAN','reason':'Evidence is complete','plan':{}},value)
        self.assertIn('complete_run',str(error.exception))
        self.assertNotIn('plan.review_again',str(error.exception))
        delivery=next(o for o in repair_options(value) if o['action']=='collect_delivery_evidence')
        self.assertIn('complete_run',delivery['meaning'])
        for mutate in (lambda v:v['tasks'][-1].update(created_graph_version=2),
                       lambda v:v['tasks'][-1].update(status='running'),
                       lambda v:v['repair_context'].update(subject_task='render')):
            changed=copy.deepcopy(value);mutate(changed)
            self.assertNotIn('complete_run',ToolProtocol(profile('media_planner'),changed,{'control_kind':'PLAN'},draft={'status':'OPEN'}).schemas)

    def test_unique_trailing_underscore_acceptance_field_is_normalized(self):
        value={'tasks':[{'type':'agent','acceptance':[{'kind':'image','height_':64,'max_participation':.5}]}]}
        normalized,changes=normalize_planner_arguments('submit_typed_plan',value)
        self.assertEqual(normalized['tasks'][0]['acceptance'][0],{'kind':'image','height':64,'max_participation':.5})
        self.assertEqual(changes,['tasks.0.acceptance.0.height_->height'])

    def test_unambiguous_operation_acceptance_is_lifted_out_of_parameters(self):
        value={'tasks':[{'type':'operation','operation':'media.edit','parameters':{
            'edit_plan':{'clips':[{'asset':'source'}]},'acceptance':[{'kind':'media','video':True}]}}]}
        normalized,changes=normalize_planner_arguments('submit_typed_plan',value)
        self.assertEqual(normalized['tasks'][0]['acceptance'],[{'kind':'media','video':True}])
        self.assertNotIn('acceptance',normalized['tasks'][0]['parameters'])
        self.assertEqual(changes,['tasks.0.parameters.acceptance->acceptance'])
        conflicting=copy.deepcopy(value);conflicting['tasks'][0]['acceptance']=[{'kind':'file'}]
        normalized,_=normalize_planner_arguments('submit_typed_plan',conflicting)
        self.assertIn('acceptance',normalized['tasks'][0]['parameters'])

    def test_structured_delivery_contract_adds_exact_names_but_not_inputs(self):
        value={'goal':'Edit source.mp4','authorization':{'delivery_targets':['video and cover']},
               'input_asset_names':['source.mp4'],**delivery(filenames=('final.mp4','cover.png'))}
        self.assertEqual(required_delivery_names(value),{'final.mp4','cover.png'})
        self.assertEqual(required_delivery_extensions(value),{'mp4','png'})

    def test_structured_exclusion_does_not_become_a_required_output(self):
        value={'goal':'Deliver final.mp4','authorization':{'delivery_targets':['video']},
               **delivery(filenames=('final.mp4',),excluded=('evidence.json',))}
        self.assertEqual(required_delivery_names(value),{'final.mp4'})

    def test_final_exclusions_override_earlier_candidate_filename_mentions(self):
        from webapp.workflow_agent_contracts import approved_delivery_refs,delivery_leaf_refs
        value={'goal':'先保存候选再交付最终视频',**delivery(filenames=('deliver.mp4',),excluded=('candidate.mp4','primary.mp4')),
            'tasks':[{'id':name,'spec':{'delivery_stage':True},'accepted_result':['edit/'+name]} for name in ('candidate.mp4','primary.mp4','deliver.mp4')]}
        self.assertEqual(required_delivery_names(value),{'deliver.mp4'})
        self.assertEqual(approved_delivery_refs(value),{'edit/deliver.mp4'})
        self.assertEqual(delivery_leaf_refs(value),{'deliver.mp4':{'edit/deliver.mp4'}})
        value.update(delivery_contract=graph.DeliveryContract(filenames=['deliver.mp4','candidate.mp4'],
            excluded_filenames=['primary.mp4']).model_dump())
        self.assertEqual(required_delivery_names(value),{'deliver.mp4','candidate.mp4'})
        self.assertEqual(approved_delivery_refs(value),{'edit/deliver.mp4','edit/candidate.mp4'})

    def test_excluded_candidate_name_cannot_be_promoted_by_global_output_rename(self):
        value=frozen(False);value.update(delivery(filenames=('deliver.mp4',),excluded=('candidate.mp4',)))
        task={'id':'candidate','type':'agent','profile':'editing_implementer','name':'Retained candidate',
            'objective':'Copy the input bytes to candidate.mp4',
            'files':[{'path':'candidate.mp4','roles':['video']}],
            'acceptance':[{'kind':'media','file':'candidate.mp4','video':True,'stage':'final'}]}
        proposal=compile_manager({'kind':'APPLY_PLAN','reason':'Retain source candidate',
            **delivery(filenames=('deliver.mp4',),excluded=('candidate.mp4',)),'plan':{'new_tasks':[task]}},value)['proposal']
        compiled=proposal['add_tasks'][0]
        self.assertEqual(compiled['outputs'],['candidate.mp4'])
        self.assertEqual(compiled['output_roles'],{'video':'candidate.mp4'})
        self.assertEqual(compiled['acceptance'][0]['file'],'candidate.mp4')

    def test_user_prose_is_not_reinterpreted_by_the_host(self):
        value={'goal':'Create internal.png; do not deliver internal.png; deliver final.mp4.'}
        self.assertEqual(required_delivery_names(value),set())
        value.update(delivery(filenames=('final.mp4',),excluded=('internal.png',)))
        self.assertEqual(required_delivery_names(value),{'final.mp4'})
        self.assertEqual(required_delivery_extensions(value),{'mp4'})

    def test_structured_final_delivery_does_not_require_intermediate_filenames(self):
        value={'goal':'生成中间图片并交付最终图片','input_asset_names':['portrait.png'],
               **delivery(filenames=('delivered.png',))}
        self.assertEqual(required_delivery_names(value),{'delivered.png'})
        value.update(delivery(filenames=('final.mp4','subtitles.srt','cover.png')))
        self.assertEqual(required_delivery_names(value),{'final.mp4','subtitles.srt','cover.png'})

    def test_internal_evidence_is_not_promoted_by_global_report_or_format_requests(self):
        from webapp.workflow_model_protocol import required_delivery_roles,bind_explicit_delivery_formats
        value={'goal':'交付处理后的音频，证据报告只供系统检查',**delivery(formats=('wav',),excluded=('evidence.json',))}
        self.assertEqual(required_delivery_roles(value),set())
        self.assertEqual(required_delivery_roles(delivery(formats=('wav',),roles=('report',))),{'report'})
        task={'purpose':'evidence','acceptance':[{'kind':'json','source':'technical','stage':'current','file':'evidence.json'}]}
        original=copy.deepcopy(task)
        bind_explicit_delivery_formats(task,['evidence.json'],delivery(formats=('wav','json'),roles=('report',)),{'report':'evidence.json'})
        self.assertEqual(task,original)

    def test_missing_delivery_bytes_remain_required_unless_explicitly_excluded(self):
        from webapp.workflow_agent_contracts import delivery_leaf_refs
        snapshot={'goal':'Deliver final.png',**delivery(filenames=('final.png',)),
                  'tasks':[{'id':'empty','spec':{'delivery_stage':True},'accepted_result':[]}]}
        self.assertEqual(delivery_leaf_refs(snapshot),{'empty':set()})

    def test_input_names_never_expand_a_structured_output_contract(self):
        value={'goal':'复用原始输入并交付成片','input_asset_names':['clip_a.mp4','recording.wav'],
               **delivery(filenames=('final.mp4','cover.png'))}
        self.assertEqual(required_delivery_names(value),{'final.mp4','cover.png'})
        self.assertEqual(required_delivery_extensions(value),{'mp4','png'})

    def test_intermediate_reused_format_is_not_a_delivery_requirement(self):
        value={'goal':'另建片段供后续复用，最终交付图片。',
               'authorization':{'delivery_targets':['128x64 PNG image']},**delivery(filenames=('final.png',))}
        self.assertEqual(required_delivery_extensions(value),{'png'})

    def test_unique_requested_basename_is_host_compiled_for_agent_revision(self):
        task={'id':'render','type':'agent','profile':'editing_implementer','name':'Export','objective':'Export video',
              'outputs':['old.mp4','cover.png'],'output_roles':{'video':'old.mp4','cover':'cover.png'},
              'acceptance':[{'kind':'media','file':'old.mp4'},{'kind':'image','file':'cover.png'}],
              'basic_reason':'export','delivery_stage':True}
        value=frozen(task,user=True)
        value['input_batch']['inputs'][0]['content']='Deliver final.mp4 and cover.png.'
        command={'kind':'COMPLETE','reason':'Apply requested names',**delivery(filenames=('final.mp4','cover.png')),
                 'plan':{'changes':[{'task_id':'render','set':{
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

    def test_applied_user_change_is_context_not_a_revision_grant(self):
        value=frozen(user=True);record=value['input_batch']['inputs'][0]
        record['context']['targets']=['render'];value['input_batch']=None;value['user_changes']=[record];value['trigger']={}
        schema=manager_schema(value)
        changes=schema['properties']['plan']['anyOf'][0]['properties']['changes']
        self.assertEqual(changes['maxItems'],0)
        self.assertEqual(scopes(value),(None,None))
        self.assertFalse(targeted_user_inputs(value))
        command={'kind':'COMPLETE','reason':'Apply retained input','plan':{'changes':[{
            'task_id':'render','set':{'objective':'Updated after the input drain'}}]}}
        with self.assertRaises(CommandError) as raised:compile_manager(command,value)
        self.assertEqual(raised.exception.code,'revision_not_available')
        value['input_batch']={'through_sequence':8,'inputs':[record]}
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
        result=compile_manager({'kind':'COMPLETE','reason':'Deliver records',**delivery(formats=('json','txt')),
                                'plan':{'new_tasks':[task]}},f)
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
        result=compile_manager({'kind':'APPLY_PLAN','reason':'Read text',**delivery(formats=('png','json')),
                                'plan':{'new_tasks':[task]}},f)
        compiled=result['proposal']['add_tasks'][0]
        finals={(check['file'],check['source']) for check in compiled['acceptance'] if check['stage']=='final'}
        self.assertEqual(finals,{('ocr.json','user')})

    def test_explicit_report_request_promotes_the_registered_report_role(self):
        from webapp.operations import catalog
        operation=next(item for item in catalog()['operations'] if item['id']=='audio.process')
        f=frozen(False);f['goal']='Deliver a WAV and the objective audio-process report';f['operations']=[operation]
        task={'id':'process','type':'operation','name':'Process','objective':'Process audio','operation':'audio.process',
              'inputs':{'source':{'asset':'asset'}},'parameters':{},'acceptance':[{'kind':'media','stage':'final'}]}
        result=compile_manager({'kind':'COMPLETE','reason':'Deliver audio and report',**delivery(formats=('wav','json'),roles=('report',)),
                                'plan':{'new_tasks':[task]}},f)
        compiled=result['proposal']['add_tasks'][0]
        finals={(check['file'],check['source']) for check in compiled['acceptance'] if check['stage']=='final'}
        self.assertIn(('audio-process-report.json','user'),finals)

    def test_equivalent_delivery_is_not_an_implicit_replacement(self):
        old={'id':'old','type':'agent','profile':'editing_implementer','name':'Old','objective':'Old attempt',
             'outputs':['old.png'],'output_roles':{'image':'old.png'},'delivery_stage':True,
             'acceptance':[{'kind':'image','file':'old.png','width':1024,'height':768,'source':'user','stage':'final'}],
             'basic_reason':'old'}
        f=frozen(old);f['graph_version']=2;f['trigger']={};f['tasks'][0].update(id='old',status='succeeded')
        new={'id':'fixed','type':'agent','profile':'editing_implementer','name':'Fixed','objective':'Fixed attempt',
             'files':[{'path':'fixed.png','roles':['image']}],
             'acceptance':[{'kind':'image','width':1024,'height':768,'stage':'final','source':'user'}]}
        for with_input in (False,True):
            for objective in ('Fixed attempt','Old attempt'):
                with self.subTest(with_input=with_input,objective=objective):
                    f['input_batch']={'through_sequence':7,'inputs':[{'context':{'allowed_revision_tasks':['old']}}]} if with_input else None
                    new['objective']=objective
                    result=compile_manager({'kind':'COMPLETE','reason':'Add a second variant','plan':{'new_tasks':[new]}},f)
                    self.assertEqual(result['proposal']['replacements'],{})
                    self.assertEqual(result['proposal']['add_tasks'][0]['objective'],objective)

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

        evidence={'id':'measure','type':'operation','name':'Measure audio','objective':'Measure the accepted audio',
            'operation':'media.evidence','purpose':'business','parameters':{'mode':'audio_envelope'},
            'inputs':{'source':{'repair_candidate':{'logical_task_id':'render','revision':2,
                'artifact_id':'b'*32,'sha256':'c'*64,'role':'video'}}},
            'acceptance':[{'kind':'json','stage':'final'}]}
        f['repair_candidates']=[{'logical_task_id':'render','revision':2,'artifact_id':'b'*32,'sha256':'c'*64,'role':'video'}]
        compiled=compile_manager({'kind':'APPLY_PLAN','reason':'Collect bounded evidence','plan':{'new_tasks':[evidence]}},f)
        task=compiled['proposal']['add_tasks'][0]
        self.assertEqual(task['purpose'],'evidence');self.assertFalse(task['delivery_stage'])
        self.assertTrue(task['acceptance']);self.assertTrue(all(item['stage']=='current' for item in task['acceptance']))

        f['tasks'].append({**f['tasks'][0],'id':'measure','status':'succeeded','spec':task,
            'accepted_result':['evidence-report.json'],'quality':{'technical_valid':True,'passed':True,'verified_evidence':{'mode':'audio_envelope'}}})
        retry=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        payload={'kind':'APPLY_PLAN','reason':'Review with completed evidence','review_again':{'render':['measure']}}
        wire=transform(payload,retry.schemas['submit_typed_plan'],encode=True)
        decoded=retry.decode('submit_typed_plan',wire)
        self.assertEqual(decoded['command']['review_again'],{'render':['measure']})
        with self.assertRaises(CommandError) as empty:
            compile_manager({'kind':'APPLY_PLAN','reason':'No-op','plan':{}},f)
        self.assertEqual(empty.exception.code,'missing_wait_work')
        self.assertIn('review_again',str(empty.exception));self.assertNotIn('continue_run',str(empty.exception))

    def test_exhausted_candidate_review_does_not_offer_unusable_evidence_work(self):
        f=frozen();f['tasks'][0].update(review_again_count=2,candidate_result=['report.txt'],
            quality={'technical_valid':True,'passed':True})
        f['repair_candidates']=[{'logical_task_id':'render'}]
        f['review_feedback']=[{'subject_task':'render','decision':{'verdict':'inconclusive','feedback':'Need more evidence'}}]
        f['tasks'].append({'id':'measure','status':'succeeded','spec':{'type':'operation','purpose':'evidence',
            'inputs':{'source':{'repair_candidate':{'logical_task_id':'render','revision':2}}}},
            'quality':{'verified_evidence':{'mode':'image'}}})
        forbidden={'review_again','collect_evidence_then_review_again'}
        self.assertFalse(forbidden & {o['action'] for o in repair_options(f)})
        f['tasks'][0]['review_again_count']=1
        self.assertTrue(forbidden <= {o['action'] for o in repair_options(f)})
        # Final delivery evidence is a separate review, not another Task review.
        f['tasks'][0]['review_again_count']=2;f['review_feedback'][0]['subject_task']=None
        self.assertIn('collect_delivery_evidence',{o['action'] for o in repair_options(f)})

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
        expected=graph.Task.model_validate({**f['tasks'][0]['spec'],'argv':['python','-c','print(1)'],
                                            'on_failure':'diagnose'}).model_dump()
        self.assertEqual(revised,expected)
        self.assertEqual(result['snapshot_digest'],'a'*64);self.assertEqual(result['base_graph_version'],3)
        self.assertEqual(result['proposal']['proposal_id'],'request-a');self.assertEqual(result['input_through_sequence'],0)
        self.assertNotIn('snapshot_digest',schema['properties'])
        command['plan']['changes'][0]['set']['acceptance']=[]
        with self.assertRaises(CommandError):compile_manager(command,f)
        with self.assertRaises(ValidationError):transform(command,schema,encode=True)

    def test_operation_revision_parameters_require_and_reuse_the_queried_contract(self):
        from webapp.operations import get as operation
        task={'id':'render','type':'operation','operation':'media.edit','implementation':'fixed_media@1',
              'name':'Render','objective':'Normalize audio','inputs':{'source':{'asset':'asset'}},
              'parameters':{'edit_plan':{'clips':[{'asset':'source','start':0,'end':2}]}},
              'acceptance':[{'kind':'media'}],'basic_reason':'registered edit'}
        value=frozen(task);value['tasks'][0].update(status='paused');value['trigger']={'task_id':'render'}
        value['operations']=[operation('media.edit').public()]
        unqueried=manager_schema(value)
        change=next(item for item in unqueried['properties']['plan']['anyOf'][0]['properties']['changes']['items']['anyOf'])
        self.assertNotIn('parameters',unqueried['$defs'][change['$ref'][8:]]['properties']['set']['properties'])
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertNotIn('submit_typed_plan',protocol.schemas);self.assertIn('contract',protocol.schemas)
        self.assertEqual(protocol.context()['model_command_contract']['required_contract_queries'],['media.edit'])
        contract=operation('media.edit').public()
        queried=manager_schema(value,{'media.edit':contract})
        change=queried['properties']['plan']['anyOf'][0]['properties']['changes']['items']['anyOf'][0]
        parameters=queried['$defs'][change['$ref'][8:]]['properties']['set']['properties']['parameters']
        self.assertIn('edit_plan',parameters['properties'])
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN'},
                              contracts={'media.edit':contract})
        self.assertIn('submit_typed_plan',protocol.schemas)
        command={'kind':'APPLY_PLAN','reason':'Enable registered normalization','plan':{'changes':[{
            'task_id':'render','set':{'parameters':{'edit_plan':{'clips':[{'asset':'source','start':0,'end':2}],
                'audio':{'normalize':True}}}}}]}}
        transform(command,queried,encode=True)

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

    def test_replacement_waits_for_invalidated_downstream_leaf_before_replanning(self):
        f=frozen();base=f['tasks'][0]['spec'];f['tasks'][0]['id']='render';f['tasks'][0]['status']='paused'
        middle={**copy.deepcopy(f['tasks'][0]),'id':'compose','status':'succeeded'}
        middle['spec']={**copy.deepcopy(base),'id':'compose','inputs':{'source':{'step':'render','role':'report'}}}
        leaf={**copy.deepcopy(f['tasks'][0]),'id':'deliver','status':'succeeded'}
        leaf['spec']={**copy.deepcopy(base),'id':'deliver','inputs':{'source':{'step':'compose','role':'report'}}}
        f['tasks']+=[middle,leaf]
        f['dependencies']=[{'upstream_task_id':'render','downstream_task_id':'compose'},
                           {'upstream_task_id':'compose','downstream_task_id':'deliver'}]
        implementation={'name':'New implementation','type':'script','argv':['python','-c','print(1)'],
                        'files':[{'path':'report.txt','roles':['report']}],'basic_reason':'Local repair'}
        result=compile_manager({'kind':'APPLY_PLAN','reason':'Replace the failed root','plan':{
            'repair_replacements':[{'task_id':'render','implementation':implementation}]}},f)['proposal']
        self.assertEqual(result['replan_after'],['deliver'])

    def test_replacement_does_not_readd_inherited_input_dependencies(self):
        for user in (False,True):
            with self.subTest(user=user):
                f=frozen(user=user)
                f['tasks'][0]['spec']['inputs']={'source':{'step':'prepare','role':'report'}}
                parent=copy.deepcopy(f['tasks'][0]);parent.update(id='prepare',status='succeeded')
                parent['spec'].update(id='prepare',inputs={})
                f['tasks'].append(parent)
                f['dependencies']=[{'upstream_task_id':'prepare','downstream_task_id':'render'}]
                implementation={'name':'Fixed implementation','type':'agent','profile':'editing_implementer',
                    'basic_reason':'Local report integrity',
                    'inputs':{'source':{'step':'prepare','role':'report'}},
                    'files':[{'path':'report.txt','roles':['report']}]}
                plan=({'new_tasks':[dict(implementation,id='fixed')],'replacements':{'render':'fixed'}} if user else
                      {'repair_replacements':[{'task_id':'render','implementation':implementation}]})
                result=compile_manager({'kind':'APPLY_PLAN','reason':'Repair using the same accepted input','plan':plan},f)['proposal']
                self.assertEqual(result['add_dependencies'],[])
                self.assertEqual(result['replan_after'],[result['replacements']['render']])
                self.assertEqual(result['add_tasks'][0]['inputs']['source']['step'],'prepare')

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

    def test_agent_replacement_preserves_intermediate_acceptance_and_filename(self):
        from webapp.operations import catalog
        op=next(item for item in catalog()['operations'] if item['id']=='image.perspective')
        spec={'id':'render','type':'operation','name':'Intermediate','objective':'Preserve the intermediate image',
              'operation':'image.perspective','implementation':op['implementation'],
              'inputs':{'source':{'asset':'asset'}},'parameters':{'points':[[0,0],[1,0],[1,1],[0,1]]},
              'acceptance':[{'kind':'agent','criterion':'Keep the source geometry','stage':'current'},
                            {'kind':'image','source':'technical','stage':'current'}],
              'delivery_stage':False}
        f=frozen(spec);f['goal']='Deliver final.png and JPEG';f['operations']=[op]
        implementation={'type':'agent','profile':'editing_implementer','name':'Repair intermediate',
            'inputs':{'source':{'asset':'asset'}},'files':[{'path':'intermediate.png','roles':['primary_image']}],
            'basic_reason':'Local image integrity'}
        result=compile_manager({'kind':'APPLY_PLAN','reason':'Repair without changing requirements','plan':{
            'repair_replacements':[{'task_id':'render','implementation':implementation}]}},f)['proposal']['add_tasks'][0]
        self.assertFalse(result['delivery_stage'])
        self.assertEqual(result['acceptance'],f['tasks'][0]['spec']['acceptance'])
        self.assertEqual(result['outputs'],['intermediate.png'])

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

    def test_non_strict_union_keeps_nullable_defaults_on_the_matching_invalid_branch(self):
        schema={'anyOf':[{'type':'null'},obj({'kind':{'type':'string','enum':['media']},
            'name':{'type':'string'},'checker':{'type':'string'}},['kind','name'])]}
        with self.assertRaises(ValidationError) as raised:transform({'kind':'media'},schema)
        missing={item['path'] for item in validation_feedback(raised.exception)}
        self.assertEqual(missing,{'name'})

    def test_planner_asset_reference_is_bound_to_frozen_authorized_ids(self):
        value=frozen(False);value['assets']=[{'id':'asset-a'},{'id':'asset-b'}]
        branch=manager_schema(value)['$defs']['InputRef']['properties']['asset']['anyOf'][0]
        self.assertEqual(branch['enum'],['asset-a','asset-b'])
        self.assertIn('exact authorized asset ID',branch['description'])

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

    def test_agent_timeout_schema_matches_the_implementer_profile_limit(self):
        from webapp.specialist_profiles import get
        schema=manager_schema(frozen(False))
        self.assertEqual(schema['$defs']['AgentCommandTask']['properties']['timeout_seconds']['maximum'],
                         get('editing_implementer').timeout_seconds)
        path=schema['$defs']['AgentCommandTask']['properties']['files']['items']['properties']['path']
        self.assertIn('never a directory',path['description'])

    def test_planner_cannot_infer_human_approval_from_task_text(self):
        for goal in ('Make the cuts sound smooth','Render a preview and let me manually review it'):
            schema=manager_schema({**frozen(False),'goal':goal})
            refs={item['$ref'] for item in schema['$defs']['Acceptance']['anyOf']}
            self.assertNotIn('#/$defs/CommandAcceptanceReview',refs)
            self.assertIn('review_policy',schema['properties'])

    def test_initial_plan_requires_ai_structured_delivery_semantics(self):
        first={**frozen(False),'graph_version':0,'goal':'Please produce the requested result'}
        second={**first,'goal':'请生成用户要求的最终成果'}
        schemas=[manager_schema(value) for value in (first,second)]
        for schema in schemas:
            self.assertIn('delivery_contract',schema['properties'])
        protocol=ToolProtocol(profile('media_planner'),first,{'control_kind':'PLAN'},draft={'status':'OPEN','draft':{}})
        schema=protocol.schemas['submit_typed_plan']
        self.assertIn('delivery_contract',schema['required'])
        self.assertNotIn('review',str(schema['$defs']['Acceptance']['anyOf']))
        self.assertNotIn('on_failure',schema['$defs']['AgentCommandTask']['properties'])
        protocol=ToolProtocol(profile('media_planner'),first,{'control_kind':'PLAN'},
                              draft={'status':'OPEN','draft':{'new_tasks':[{'id':'existing'}]}})
        commit=protocol.schemas['commit_plan']
        self.assertIn('delivery_contract',commit['required']);self.assertIn('DeliveryContract',commit['$defs'])
        with self.assertRaises(CommandError) as rejected:
            compile_manager({'kind':'COMPLETE','reason':'Incomplete semantics','delivery_contract':graph.DeliveryContract().model_dump(),
                             'plan':{'new_tasks':[]}},first)
        self.assertEqual((rejected.exception.code,rejected.exception.path),('delivery_contract_required','delivery_contract'))

    def test_respond_tool_cannot_be_described_as_pre_execution_planning(self):
        value={**frozen(False),'graph_version':0,'can_respond':True}
        protocol=ToolProtocol(profile('media_planner'),value,{'control_kind':'PLAN'},draft={'status':'OPEN','draft':{}})
        definition=next(item for item in protocol.definitions() if item['name']=='respond')
        self.assertIn('every part of the request',definition['description'])
        self.assertIn('work is not executed',definition['description'])

    def test_transcript_subtitles_cannot_also_claim_an_uploaded_srt_role(self):
        from webapp.operation_parameters import EditSubtitles
        with self.assertRaisesRegex(ValueError,'uploaded SRT'):
            EditSubtitles(asset='transcript',from_transcripts=True)

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
        self.assertEqual(list(p.schemas),['submit_typed_plan','request_user_input','request_authorization','new_run_required'])
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
        self.assertEqual(model_output_limit(profile('media_planner'),final),6144)

    def test_plan_draft_control_result_binds_the_current_draft(self):
        f=frozen(user=True);draft={'status':'OPEN','revision':3,'draft':{
            'new_tasks':[],'changes':[],'dependencies':[],'remove_dependencies':[],
            'repair_replacements':[],'replacements':{},'review_again':{},'_commands':['a','b','c']}}
        protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft=draft)
        wire=transform({'reason':'No graph change is needed'},protocol.schemas['continue_run'],encode=True)
        result=protocol.decode('continue_run',wire)
        self.assertEqual(result['_draft_revision'],3)
        self.assertEqual(result['_draft_digest'],graph.digest(draft['draft']))

    def test_finalize_keeps_typed_submission_when_the_rejected_draft_is_empty(self):
        f=frozen(False);draft={'status':'OPEN','revision':0,'last_error_signature':'compile-error','draft':{
            'new_tasks':[],'changes':[],'dependencies':[],'remove_dependencies':[],
            'repair_replacements':[],'replacements':{},'review_again':{},'_commands':[]}}
        protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft=draft,finalize=True)
        self.assertIn('submit_typed_plan',protocol.schemas)
        self.assertIn('commit_plan',protocol.schemas)

    def test_continue_is_unavailable_without_a_frozen_input_batch(self):
        f=frozen(False);protocol=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        self.assertNotIn('continue_run',protocol.schemas)
        raw={'kind':'CONTINUE','reason':'Invalid no-op','snapshot_digest':f['snapshot_digest'],
             'base_graph_version':f['graph_version'],'authorization_version':f['authorization_version'],
             'input_through_sequence':0}
        with self.assertRaisesRegex(ValueError,'frozen input batch'):graph.ManagerResult.model_validate(raw)

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

    def test_scope_tools_require_non_null_authorization_in_the_wire_schema(self):
        from jsonschema import Draft202012Validator
        p=ToolProtocol(profile('media_planner'),frozen(False),{'control_kind':'PLAN'},draft={'status':'OPEN'})
        for name in ('request_authorization','new_run_required'):
            with self.subTest(tool=name):
                payload={'reason':'Change scope','authorization':None}
                if name=='new_run_required':payload['new_goal']='New confirmed goal'
                self.assertFalse(Draft202012Validator(schema_for(p.schemas[name])).is_valid(payload))
                with self.assertRaises(ValidationError):p.decode(name,payload)

    def test_successor_tool_binds_single_new_goal_without_retyping_scope_goal(self):
        f=frozen(False);f['goal']='Original immutable goal'
        p=ToolProtocol(profile('media_planner'),f,{'control_kind':'PLAN'},draft={'status':'OPEN'})
        source=p.schemas['new_run_required']
        auth=graph.Authorization(goal='New goal',allowed_assets=['asset-a'],allowed_operations=[],
            allowed_agent_profiles=['editing_implementer'],delivery_targets=['new video'],content_fingerprint='b'*64).model_dump()
        auth.pop('goal')
        payload={'reason':'User changed the global target','new_goal':'New goal','authorization':auth}
        wire=transform(payload,source,encode=True)
        result=p.decode('new_run_required',wire)['proposal']
        self.assertEqual(result['authorization']['goal'],'New goal')
        self.assertEqual(result['new_goal'],'New goal')
        self.assertEqual(result['authorization']['allowed_assets'],['asset-a'])
        self.assertEqual(result['snapshot_digest'],f['snapshot_digest'])
        self.assertEqual(result['kind'],'NEW_RUN_REQUIRED')
        self.assertIsNone(result['proposal'])
        for bad in ('', '   '):
            with self.subTest(goal=bad),self.assertRaises(ValidationError):
                p.decode('new_run_required',{**wire,'new_goal':bad})
        with self.assertRaises(ValidationError):
            transform({**payload,'authorization':{**auth,'goal':'Conflicting goal'}},source,encode=True)

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

    def test_user_replacement_inherits_requirements_instead_of_model_retyping(self):
        value=frozen(user=True);old=value['tasks'][0]['spec']
        replacement={'id':'replacement','type':'agent','profile':'editing_implementer','name':'Replacement renderer',
            'objective':'Weakened model objective','input_bindings':[],
            'files':[{'path':'report.txt','roles':['report']}],
            'acceptance':[{'kind':'file'}],'semantics':{'consistency_mode':'creative'},'basic_reason':'Change implementation'}
        command={'kind':'COMPLETE','reason':'Replace implementation','plan':{
            'new_tasks':[replacement],'replacements':{'render':'replacement'}}}
        compiled=compile_manager(command,value)['proposal']['add_tasks'][0]
        for field in ('objective','acceptance','semantics','delivery_stage'):
            self.assertEqual(compiled[field],old[field],field)

    def test_replacement_compiles_new_downstream_references_to_the_new_task(self):
        value=frozen(user=True)
        replacement={'id':'replacement','type':'agent','profile':'editing_implementer','name':'Replacement renderer',
            'objective':'Render','input_bindings':[],'files':[{'path':'report.txt','roles':['report']}],
            'acceptance':[{'kind':'file'}],'basic_reason':'Change implementation'}
        downstream={'id':'summary','type':'agent','profile':'editing_implementer','name':'Summarize',
            'objective':'Summarize the report','input_bindings':[{'role':'source','ref':{'step':'render','role':'report'}}],
            'files':[{'path':'summary.txt','roles':['summary']}],'acceptance':[{'kind':'file','stage':'final'}],
            'basic_reason':'Produce final summary'}
        proposal=compile_manager({'kind':'COMPLETE','reason':'Replace and continue','plan':{
            'new_tasks':[replacement,downstream],'replacements':{'render':'replacement'}}},value)['proposal']
        summary=next(task for task in proposal['add_tasks'] if task['id']=='summary')
        self.assertEqual(summary['inputs']['source']['step'],'replacement')
        self.assertIn({'upstream':'replacement','downstream':'summary'},proposal['add_dependencies'])

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

    def test_manager_declares_code_runtime_gpu_dependencies_and_exact_network_grants(self):
        f=frozen(False);schema=manager_schema(f)
        task={'id':'coded','type':'agent','profile':'editing_implementer','name':'Code edit',
            'objective':'Use the approved public data to generate the result',
            'input_bindings':[{'role':'source','ref':{'asset':'asset'}}],
            'files':[{'path':'result.json','roles':['report']}],
            'acceptance':[{'kind':'json','required_keys':['value'],'stage':'final'}],
            'basic_reason':'Requires generated local implementation',
            'runtime_requirements':{'requires_gpu':False,'python_modules':['numpy'],
                'node_modules':['three'],'system_tools':['ffmpeg'],'network_urls':['https://example.com/data.json']}}
        command={'kind':'COMPLETE','reason':'Run the portable code editor','plan':{'new_tasks':[task]}}
        result=compile_manager(transform(transform(command,schema,encode=True),schema),f)['proposal']['add_tasks'][0]
        self.assertEqual(result['runtime_requirements'],task['runtime_requirements'])
        task['runtime_requirements']['requires_gpu']=True
        result=compile_manager(transform(transform(command,schema,encode=True),schema),f)['proposal']['add_tasks'][0]
        self.assertTrue(result['runtime_requirements']['requires_gpu'])

    def test_review_uses_only_actual_inspection_ids_and_host_snapshot(self):
        from webapp.workflow_agent_contracts import ReviewDecision
        f=frozen();f.update(result_schema=ReviewDecision.model_json_schema(),review_scope='task')
        p=ToolProtocol(profile('delivery_reviewer'),f,{'control_kind':'REVIEW'},inspections={'b'*32:'immutable.png'})
        value={'verdict':'pass','evidence':[{'criterion_index':0,'method_use':'not_applicable','observation':'Checked','inspection_id':'b'*32}]}
        wire=transform(value,p.schemas['submit_result'],encode=True)
        result=p.decode('submit_result',wire)
        self.assertEqual(result['decision']['snapshot_digest'],'a'*64)
        wire['evidence'][0]['inspection_id']='c'*32
        with self.assertRaises(ValidationError):p.decode('submit_result',wire)
        supporting=copy.deepcopy(value);supporting['evidence'][0]['supporting_inspection_ids']=['b'*32]
        decoded=p.decode('submit_result',supporting)
        self.assertEqual(decoded['decision']['evidence'][0]['supporting_inspection_ids'],['b'*32])

    def test_review_tool_schema_requires_the_exact_frozen_criterion_count(self):
        from webapp.workflow_agent_contracts import ReviewDecision
        f=frozen();f.update(result_schema=ReviewDecision.model_json_schema(),review_scope='final',
                            review_criteria=['first','second','third'])
        p=ToolProtocol(profile('delivery_reviewer'),f,{'control_kind':'REVIEW'},inspections={'b'*32:'immutable.png'})
        evidence=p.schemas['submit_result']['properties']['evidence']
        self.assertEqual((evidence['minItems'],evidence['maxItems']),(3,3))
        self.assertEqual(p.schemas['submit_result']['$defs']['Evidence']['properties']['criterion_index']['enum'],[0,1,2])

    def test_review_method_facts_are_required_and_cannot_support_a_false_pass(self):
        from webapp.workflow_agent_contracts import ReviewDecision,resolve_review,validate_resolved_review
        snapshot={'snapshot_digest':'a'*64,'review_scope':'task','review_criteria':['Use the required method output'],
                  'result_schema':ReviewDecision.model_json_schema()}
        inspection='b'*32;refs={inspection:'edit/result.png'}
        protocol=ToolProtocol(profile('delivery_reviewer'),snapshot,{'control_kind':'REVIEW'},inspections=refs)
        evidence={'criterion_index':0,'inspection_id':inspection,'observation':'Only checked the candidate extent'}
        for use in ('reference_only','unverified'):
            with self.subTest(method_use=use):
                value={'verdict':'pass','evidence':[{**evidence,'method_use':use}]}
                # The wire schema permits honest facts. Domain validation rejects
                # the incompatible passing conclusion, not the facts themselves.
                decoded=protocol.decode('submit_result',transform(value,protocol.schemas['submit_result'],encode=True))['decision']
                with self.assertRaisesRegex(ValueError,'方法契约未满足'):ReviewDecision.model_validate(decoded)
                rejected=ReviewDecision.model_validate({**decoded,'verdict':'reject'}).model_dump()
                resolved=resolve_review(rejected,snapshot,refs)
                self.assertEqual(resolved['evidence'][0]['method_use'],use)
                with self.assertRaisesRegex(ValueError,'方法契约未满足'):
                    validate_resolved_review({**resolved,'verdict':'pass'},snapshot,refs)
        for use in ('not_applicable','output_content'):
            accepted=ReviewDecision.model_validate({'snapshot_digest':'a'*64,'verdict':'pass',
                'evidence':[{**evidence,'method_use':use}]}).model_dump()
            resolved=resolve_review(accepted,snapshot,refs)
            validate_resolved_review(resolved,snapshot,refs)
            self.assertEqual(resolved['evidence'][0]['method_use'],use)
        with self.assertRaises(ValidationError):
            transform({'verdict':'pass','evidence':[evidence]},protocol.schemas['submit_result'],encode=True)

    def test_final_review_selects_short_candidate_keys_not_internal_ids_or_paths(self):
        from webapp.workflow_agent_contracts import ReviewDecision
        f=frozen();f.update(result_schema=ReviewDecision.model_json_schema(),review_scope='final')
        inspections={'b'*32:'edit/final.png'};keys={'delivery_1':'b'*32}
        f['tasks'][0].update(accepted_result=['edit/final.png'],quality={'output_roles':{'primary_image':'edit/final.png'}})
        p=ToolProtocol(profile('delivery_reviewer'),f,{'control_kind':'REVIEW'},inspections=inspections,candidate_keys=keys)
        value={'verdict':'pass','evidence':[{'criterion_index':0,'method_use':'not_applicable','observation':'Checked','inspection_id':'b'*32}],
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

    def test_final_review_with_empty_delivery_targets_uses_frozen_goal_criterion(self):
        from webapp.workflow_agent_contracts import resolve_review
        snapshot={'review_scope':'final','goal':'交付符合要求的最终视频','delivery_targets':[],
                  'review_criteria':['交付符合要求的最终视频'],'tasks':[{'id':'render','spec':{'delivery_stage':True},
                  'accepted_result':['edit/final.mp4'],'quality':{'output_roles':{'video':'edit/final.mp4'}}}]}
        inspection='b'*32;decision={'snapshot_digest':'a'*64,'verdict':'pass',
            'evidence':[{'criterion_index':0,'method_use':'not_applicable','observation':'已检查成片','inspection_id':inspection}],
            'deliverables':['delivery_1'],'primary':'delivery_1','feedback':''}
        resolved=resolve_review(decision,snapshot,{inspection:'edit/final.mp4'},{'delivery_1':inspection})
        self.assertEqual(resolved['evidence'][0]['criterion'],snapshot['review_criteria'][0])
        self.assertEqual(resolved['primary'],'edit/final.mp4')
        bad={**decision,'evidence':[{'criterion_index':1,'method_use':'not_applicable','observation':'越界','inspection_id':inspection}]}
        with self.assertRaisesRegex(ValueError,'0..0'):
            resolve_review(bad,snapshot,{inspection:'edit/final.mp4'},{'delivery_1':inspection})

    def test_input_asset_filename_is_not_a_required_delivery_format(self):
        from webapp.workflow_model_protocol import required_delivery_extensions
        frozen_review={'goal':'Use music.wav under the video and deliver MP4',
                       'authorization':{'delivery_targets':['complete video and requested attachments']},
                       'input_asset_names':['music.wav'],**delivery(formats=('mp4',))}
        self.assertEqual(required_delivery_extensions(frozen_review),{'mp4'})
        frozen_review.update(delivery(formats=('mp4','wav')))
        self.assertEqual(required_delivery_extensions(frozen_review),{'mp4','wav'})

    def test_final_filename_rename_replaces_the_previous_name(self):
        frozen={'goal':'最终文件改名后交付','authorization':{'delivery_targets':['128x64 PNG']},
                **delivery(filenames=('renamed.png',))}
        self.assertEqual(required_delivery_names(frozen),{'renamed.png'})

    def test_explicit_non_delivery_filename_does_not_create_a_format_requirement(self):
        from webapp.workflow_model_protocol import required_delivery_extensions
        frozen={'goal':'保存参考图但只交付视频','authorization':{'delivery_targets':['符合需求的视频及要求的附加文件']},
                **delivery(formats=('mp4',),excluded=('reference.png',))}
        self.assertEqual(required_delivery_extensions(frozen),{'mp4'})
        frozen.update(delivery(formats=('mp4','png'),excluded=('reference.png',)))
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
