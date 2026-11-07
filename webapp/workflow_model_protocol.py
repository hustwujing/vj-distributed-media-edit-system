"""Model-facing commands compiled into the unchanged, fenced domain contracts."""
from copy import deepcopy
from . import workflow_graph as graph
from .strict_schema import obj,schema_for,transform


class CommandError(ValueError):
    def __init__(self,code,path,message):
        self.code=code;self.path=path
        super().__init__(message)


def normalize_planner_arguments(name,arguments,dynamic_operation_inputs=()):
    """Normalize only legacy/redundant planner shapes with one exact meaning."""
    if name!='submit_typed_plan' and not name.startswith(('replace_agent_tasks','replace_operation_tasks_')):
        return arguments,[]
    value=deepcopy(arguments);changes=[]
    from .quality_policy import Acceptance
    acceptance_fields=set(Acceptance.model_fields)
    def normalize_acceptance_fields(node,path=''):
        if isinstance(node,dict):
            if node.get('kind') in ('file','image','media','text','json','subtitle','review','agent'):
                for key in list(node):
                    canonical=key[:-1] if key.endswith('_') else ''
                    if canonical in acceptance_fields and canonical not in node:
                        node[canonical]=node.pop(key);changes.append((path+'.' if path else '')+key+'->'+canonical)
            for key,item in node.items():normalize_acceptance_fields(item,(path+'.' if path else '')+key)
        elif isinstance(node,list):
            for index,item in enumerate(node):normalize_acceptance_fields(item,(path+'.' if path else '')+str(index))
    normalize_acceptance_fields(value)
    plan=value
    if not isinstance(plan,dict):return value,changes
    tasks=plan.get('tasks')
    def binding_map(items):
        mapped={}
        for item in items:
            if not isinstance(item,dict):return None
            binding=None;ref=None
            if set(item)=={'key','value'}:binding,ref=item['key'],item['value']
            elif len(item)==1 and isinstance(next(iter(item.values())),dict):binding,ref=next(iter(item.items()))
            elif isinstance(item.get('ref'),dict):binding,ref=item.get('input_role',item.get('name',item.get('role'))),item['ref']
            elif isinstance(item.get('value'),dict):binding,ref=item.get('input_role',item.get('name',item.get('key'))),item['value']
            else:
                binding=item.get('input_role',item.get('name'))
                if binding:
                    ref={key:item[key] for key in ('asset','step','role','repair_candidate') if key in item}
                elif item.get('role') and 'asset' in item:
                    binding=item['role'];ref={'asset':item['asset']}
            if not isinstance(binding,str) or not isinstance(ref,dict) or binding in mapped:return None
            mapped[binding]=ref
        return mapped
    for index,task in enumerate(tasks if isinstance(tasks,list) else []):
        if not isinstance(task,dict):continue
        kind=task.get('type')
        if kind=='operation':
            bindings=task.get('inputs')
            if task.get('operation') in dynamic_operation_inputs and isinstance(bindings,(dict,list)):
                mapped=bindings if isinstance(bindings,dict) else binding_map(bindings)
                if mapped is not None:
                    task.pop('inputs');task['input_bindings']=[{'role':role,'ref':ref} for role,ref in mapped.items()]
                    changes.append(f'tasks.{index}.operation_inputs_mapped')
            elif isinstance(bindings,list):
                mapped=binding_map(bindings)
                if mapped is not None:task['inputs']=mapped;changes.append(f'tasks.{index}.operation_inputs_mapped')
        elif kind in ('agent','script') and 'input_bindings' not in task and isinstance(task.get('inputs'),dict):
            task['input_bindings']=[{'role':role,'ref':ref} for role,ref in task.pop('inputs').items()]
            changes.append(f'tasks.{index}.inputs_mapped')
    return value,changes


def scopes(frozen):
    repair=frozen.get('repair_context') or {};trigger=frozen.get('trigger') or {}
    automatic=(repair.get('subject_task','') if repair.get('active') else trigger.get('task_id')
               if 'task_id' in trigger else '' if any(k in trigger for k in ('review_rejected','task_review_rejected')) else None)
    current=(frozen.get('input_batch') or {}).get('inputs') or []
    # Applied input is historical context, never a standing revision grant.
    user=set().union(*(set(item['context']['allowed_revision_tasks']) for item in current)) if current else None
    return automatic,user


def targeted_user_inputs(frozen):
    records=(frozen.get('input_batch') or {}).get('inputs') or []
    _,user=scopes(frozen)
    return user is not None and bool(records) and all((item.get('context') or {}).get('targets') for item in records)


def change_fields(task_type,user=False):
    return graph.revision_mutable_fields(task_type,user)


REPLACEMENT_REQUIREMENT_FIELDS=('objective','acceptance','semantics','delivery_stage')


def _delivery_request_text(frozen):
    import re
    records=[*frozen.get('user_changes',[]),*((frozen.get('input_batch') or {}).get('inputs') or [])]
    changes=[item.get('content','') for item in records if isinstance(item,dict)]
    text=' '.join([frozen.get('goal',''),*frozen.get('authorization',{}).get('delivery_targets',[]),*changes])
    for value in frozen.get('input_asset_names',[]):
        if value:text=re.sub(re.escape(value),' ',text,flags=re.IGNORECASE)
    for asset in frozen.get('assets',[]):
        for value in (asset.get('name'),asset.get('filename')):
            if value:text=re.sub(re.escape(value),' ',text,flags=re.IGNORECASE)
    extension=r'(?:png|jpe?g|mp4|wav|mp3|srt|json|txt|pdf|zip|psd)'
    filename=re.compile(r'(?i)(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.'+extension+r'(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])')
    def omit_non_delivery(match):
        after=text[match.end():]
        endings=[index for mark in ';；。\n' if (index:=after.find(mark))>=0]
        nearby=match.group(0)+after[:min(endings) if endings else len(after)]
        excluded=r'(?:非交付|不交付|不要交付|无需交付|不得交付|仅(?:作为|供)?(?:中间|内部|参考)|non[- ]?deliverable|do not deliver|not for delivery|internal only)'
        return ' ' if re.search(excluded,nearby,re.IGNORECASE) else match.group(0)
    text=filename.sub(omit_non_delivery,text)
    format_token=re.compile(r'(?i)(?<![A-Za-z0-9])(PNG|JPE?G|MP4|WAV|MP3|SRT|JSON|TXT|PDF|ZIP|PSD)(?![A-Za-z0-9])')
    def omit_intermediate_format(match):
        after=text[match.end():]
        endings=[index for mark in ';；。\n' if (index:=after.find(mark))>=0]
        clause=after[:min(endings) if endings else len(after)]
        intermediate=r'(?:中间|复用|输入|素材|备用|参考|intermediate|reuse|input|source|reference)'
        return ' ' if re.search(intermediate,clause,re.IGNORECASE) else match.group(0)
    return format_token.sub(omit_intermediate_format,text),filename


def required_delivery_names(frozen):
    """Return explicit output basenames from the confirmed scope and applied input."""
    import re
    text,filename=_delivery_request_text(frozen)
    matches=list(filename.finditer(text));names={match.group(0) for match in matches}
    name=r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:png|jpe?g|mp4|wav|mp3|srt|json|txt|pdf|zip|psd)'
    for renamed in re.finditer(rf'(?i)(?P<old>{name})\s*(?:改名为|重命名为|rename(?:d)?\s+to)\s*(?P<new>{name})',text):
        names.discard(renamed.group('old'))
    for renamed in re.finditer(rf'(?i)(?:最终文件|最终产物|交付文件|final\s+(?:file|output))\s*(?:改名为|重命名为|rename(?:d)?\s+to)\s*(?P<new>{name})',text):
        target=renamed.group('new');extension=target.rsplit('.',1)[-1].lower()
        for match in matches:
            if match.start()<renamed.start() and match.group(0).rsplit('.',1)[-1].lower()==extension:names.discard(match.group(0))
        names.add(target)
    return names


def required_delivery_extensions(frozen):
    import re
    text,_=_delivery_request_text(frozen)
    aliases={'jpg':'jpeg'}
    return {aliases.get(value.lower(),value.lower()) for value in re.findall(
        r'(?i)(?<![A-Za-z0-9])(PNG|JPE?G|MP4|WAV|MP3|SRT|JSON|TXT|PDF|ZIP|PSD)(?![A-Za-z0-9])',text)}


def bind_explicit_delivery_names(raw,filenames,roles,frozen):
    """Compile one unambiguous requested basename into an Agent file contract."""
    from pathlib import PurePosixPath
    aliases={'jpg':'jpeg'}
    def extension(name):
        suffix=PurePosixPath(name).suffix.lstrip('.').lower()
        return aliases.get(suffix,suffix)
    requested={}
    for name in required_delivery_names(frozen):requested.setdefault(extension(name),[]).append(name)
    candidates={}
    for name in filenames:candidates.setdefault(extension(name),[]).append(name)
    replacements={values[0]:requested[kind][0] for kind,values in candidates.items()
                  if len(values)==1 and len(requested.get(kind,[]))==1
                  and PurePosixPath(values[0]).name!=requested[kind][0]}
    for old,new_name in replacements.items():
        path=PurePosixPath(old);new=str(path.with_name(new_name))
        filenames[filenames.index(old)]=new
        for role,value in list(roles.items()):
            if value==old:roles[role]=new
        for check in raw.get('acceptance',[]):
            if check.get('file')==old:check['file']=new


def required_delivery_roles(frozen):
    import re
    text=' '.join([frozen.get('goal',''),*frozen.get('authorization',{}).get('delivery_targets',[])])
    pattern=r'(?:交付|输出|保存|附上|deliver|provide|include|output|save|attach).{0,48}(?:报告|\breport\b)'
    return {'report'} if re.search(pattern,text,re.IGNORECASE) else set()


def bind_explicit_delivery_formats(raw,filenames,frozen,role_filenames=None):
    aliases={'jpg':'jpeg'};required=required_delivery_extensions(frozen);acceptance=list(raw.get('acceptance',[]))
    matches=[]
    for name in filenames:
        if '.' not in name:continue
        extension=aliases.get(name.rsplit('.',1)[-1].lower(),name.rsplit('.',1)[-1].lower())
        if extension in required:matches.append(name)
    for name in matches:
        existing=[check for check in acceptance if check.get('file') in (name,'*')]
        user=next((check for check in existing if check.get('source','user')=='user'),None)
        if user:user['stage']='final';user['file']=name
        elif existing:acceptance.append({**existing[0],'source':'user','stage':'final','file':name})
    for role in required_delivery_roles(frozen):
        name=(role_filenames or {}).get(role)
        if not name:continue
        existing=[check for check in acceptance if check.get('file') in (name,role,'*') and check.get('kind') in ('json','text','file')]
        user=next((check for check in existing if check.get('source','user')=='user'),None)
        if user:user['stage']='final';user['file']=name
        elif existing:acceptance.append({**existing[0],'source':'user','stage':'final','file':name})
    raw['acceptance']=acceptance


def operation_acceptance(contract,parameters):
    from .operations import active_output_roles
    roles=active_output_roles(contract,parameters)
    filenames=contract.get('output_contract',{}).get('filenames')
    fixed=dict(zip(roles,filenames)) if filenames and len(filenames)==len(roles) else {}
    result=[]
    for role in roles:
        if role in ('primary_image','foreground','mask','preview'):kind='image'
        elif role in ('video','audio'):kind='media'
        elif role in ('report','plan','transcript'):kind='json'
        elif role=='subtitle':kind='subtitle'
        else:kind='file'
        check={'kind':kind,'source':'technical','file':fixed.get(role,'*')}
        if kind=='media':check['decode']=True;check['video']=role=='video'
        if check not in result:result.append(check)
    return result or [{'kind':'file','source':'technical'}]


def referenced_schema(root,definitions):
    """Copy only the transitive local definitions used by one command."""
    selected={}
    def visit(value):
        if isinstance(value,dict):
            ref=value.get('$ref','')
            if isinstance(ref,str) and ref.startswith('#/$defs/'):
                name=ref[8:]
                if name not in selected:
                    selected[name]=None;selected[name]=deepcopy(definitions[name]);visit(selected[name])
            for item in value.values():visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
    result=deepcopy(root);visit(result)
    if selected:result['$defs']=selected
    return result


def schema_complexity(definitions):
    import json
    value=definitions if isinstance(definitions,list) else [definitions]
    totals={'schema_bytes':len(json.dumps(value,sort_keys=True,separators=(',',':')).encode()),
            'schema_max_depth':0,'schema_object_count':0,'schema_union_branches':0,'schema_enum_values':0}
    def walk(node,depth=0):
        totals['schema_max_depth']=max(totals['schema_max_depth'],depth)
        if isinstance(node,dict):
            if node.get('type')=='object':totals['schema_object_count']+=1
            totals['schema_union_branches']+=sum(len(node.get(key,[])) for key in ('anyOf','oneOf'))
            totals['schema_enum_values']+=len(node.get('enum',[]))
            for child in node.values():walk(child,depth+1)
        elif isinstance(node,list):
            for child in node:walk(child,depth+1)
    walk(value)
    return totals


def available_changes(frozen):
    automatic,user=scopes(frozen);tasks={t['id']:t for t in frozen.get('tasks',[])}
    allowed=set(user) if user is not None else set(tasks) if automatic=='' else {automatic} if automatic else set()
    if user is None and automatic:
        while True:
            expanded=allowed|{edge['upstream_task_id'] for edge in frozen.get('dependencies',[]) if edge['downstream_task_id'] in allowed}
            if expanded==allowed:break
            allowed=expanded
    return {name:change_fields(row['spec']['type'],user is not None) for name,row in tasks.items()
            if name in allowed and row['status'] in (('paused','succeeded','ready','blocked','failed') if user is not None else ('paused','succeeded'))}


def repair_options(frozen):
    changes=available_changes(frozen);tasks={item['id']:item for item in frozen.get('tasks',[])};options=[]
    candidate_subjects={item['logical_task_id'] for item in frozen.get('repair_candidates',[])}
    for identifier,fields in changes.items():
        task=tasks[identifier]
        if task['spec']['type']=='agent' and not fields:
            options.append({'action':'rerun_agent_revision','task_id':identifier,'changes_set':{},
                            'meaning':'Create a new immutable revision with the same Agent contract and current repair context.'})
        else:options.append({'action':'revise_task','task_id':identifier,'allowed_set_fields':fields})
    for subject,row in tasks.items():
        if (not row.get('quality',{}).get('technical_valid') or not row.get('quality',{}).get('passed')
                or not (row.get('accepted_result') or subject in candidate_subjects)):continue
        evidence=[]
        for identifier,item in tasks.items():
            source=item['spec'].get('inputs',{}).get('source',{}).get('repair_candidate')
            if (item['status']=='succeeded' and item['spec'].get('purpose')=='evidence' and item.get('quality',{}).get('verified_evidence')
                    and source and source['logical_task_id']==subject and source['revision']==row['revision']):evidence.append(identifier)
        if evidence:options.append({'action':'review_again','task_id':subject,'eligible_evidence_tasks':sorted(evidence)})
    for review in frozen.get('review_feedback',[]):
        decision=review.get('decision',{});subject=review.get('subject_task')
        if decision.get('verdict')!='inconclusive':continue
        text=(decision.get('feedback') or '').lower();modes=[]
        if any(word in text for word in ('fade','淡出','淡入','rms','响度','音量包络')):modes.append('audio_envelope')
        if any(word in text for word in ('mix','混音','原声','music','声源')):modes.append('audio_compare')
        if any(word in text for word in ('pixel','像素','边距','位置','frame','帧')):modes.append('video_window')
        subjects=[subject] if subject in tasks else [identifier for identifier,item in tasks.items()
            if item['spec'].get('delivery_stage') and (item.get('accepted_result') or identifier in candidate_subjects)]
        for identifier in subjects:
            options.append({'action':'collect_evidence_then_review_again','task_id':identifier,
                            'operation':'media.evidence','suggested_modes':list(dict.fromkeys(modes or ['auto'])),
                            'meaning':'Keep the accepted candidate immutable. Add evidence Tasks for the missing facts, then use review_again; do not rerender unchanged media.'})
    return options


def evidence_collection_available(frozen):
    return any(item['action']=='collect_evidence_then_review_again' for item in repair_options(frozen))


def completion_candidate(frozen):
    """Offer sealing for a successful batch, without assuming the goal is complete."""
    tasks=frozen.get('tasks',[]);trigger=frozen.get('trigger') or {}
    context=frozen.get('repair_context') or {}
    final_evidence=(trigger.get('repair_resume')=='review_evidence' and context.get('active')
        and context.get('source')=='review_evidence' and not context.get('subject_task')
        and any(item.get('spec',{}).get('purpose')=='evidence' and item.get('status')=='succeeded'
                and item.get('created_graph_version',-1)>context.get('opened_graph_version',0) for item in tasks))
    return ((set(trigger)=={'completed'} and not context.get('active') or final_evidence)
            and tasks and all(item.get('status')=='succeeded' for item in tasks)
            and any(item.get('spec',{}).get('delivery_stage') for item in tasks)
            and not frozen.get('input_batch'))


def manager_schema(frozen,contracts=None,*,allow_script=True):
    contracts=contracts or {}
    domain=graph.ManagerResult.model_json_schema();defs=deepcopy(domain['$defs']);task=defs['Task'];props=task['properties']
    from .quality_policy import Acceptance
    acceptance=defs['Acceptance']['properties'];variants=[]
    for kind in ('file','image','media','text','json','subtitle','agent','review'):
        fields={key:deepcopy(acceptance[key]) for key in sorted(Acceptance.fields_for_kind(kind)-{'source'})}
        fields['kind']={'type':'string','enum':[kind]};required=['kind']
        if kind in ('agent','review'):
            fields['severity']={'type':'string','enum':['required']};fields['criterion']['minLength']=1;required.append('criterion')
        if kind=='text':fields['contains']['minItems']=1;required.append('contains')
        name='CommandAcceptance'+kind.title();defs[name]=obj(fields,required);variants.append({'$ref':'#/$defs/'+name})
    defs['Acceptance']={'anyOf':variants}
    files={'type':'array','minItems':1,'maxItems':64,'items':obj({'path':{'type':'string','pattern':'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$'},
           'roles':{'type':'array','minItems':1,'items':{'type':'string','pattern':'^[a-z][a-z0-9_-]{0,39}$'},'maxItems':32}}),
           'description':'Declare each local output file once and its roles. The server derives outputs/output_roles.'}
    input_bindings={'type':'array','maxItems':100,'uniqueItems':True,'items':obj({
        'role':{'type':'string','pattern':'^[a-z][a-z0-9_-]{0,39}$'},'ref':{'$ref':'#/$defs/InputRef'}}),
        'description':'Declare each input role once. The host derives the authoritative inputs map.'}
    task_variants=[]
    irrelevant={'script':{'operation','implementation','parameters','profile'},
                'operation':{'argv','outputs','output_roles','profile','preflight','config_files'},
                'agent':{'argv','operation','implementation','parameters','preflight','config_files'}}
    for kind,removed in irrelevant.items():
        if kind=='script' and not allow_script:continue
        fields={key:deepcopy(value) for key,value in props.items() if key not in removed|{'outputs','output_roles','delivery_stage'}}
        fields['type']={'type':'string','enum':[kind]}
        if kind=='agent':
            from .specialist_profiles import PROFILES
            fields['purpose']={'type':'string','enum':['business','repair']}
            fields['profile']={'type':'string','enum':sorted(p.id for p in PROFILES.values() if p.role=='IMPLEMENTER')}
        if kind!='operation':
            fields.pop('inputs',None);fields['input_bindings']=deepcopy(input_bindings);fields['files']=files
        required=[key for key in task.get('required',[]) if key in fields]
        if kind=='agent':required.append('profile')
        if kind!='operation':required.append('files')
        if kind=='operation':
            for index,(identifier,contract) in enumerate(sorted(contracts.items())):
                bound=deepcopy(fields);bound['operation']={'type':'string','enum':[identifier]};bound.pop('implementation',None)
                parameters=deepcopy(contract['parameters_schema']);prefix='Operation'+str(index)+'_'
                if contract.get('evidence_only'):
                    bound['purpose']={'type':'string','enum':['evidence']}
                else:bound['purpose']={'type':'string','enum':['business','repair']}
                def rename(value):
                    if isinstance(value,dict):return {key:('#/$defs/'+prefix+item[8:] if key=='$ref' and isinstance(item,str) and item.startswith('#/$defs/') else rename(item)) for key,item in value.items()}
                    if isinstance(value,list):return [rename(x) for x in value]
                    return value
                for key,value in parameters.pop('$defs',{}).items():defs[prefix+key]=rename(value)
                bound['parameters']=rename(parameters)
                inputs=contract['input_contract'];roles=inputs.get('allowed_roles');input_field='inputs'
                if roles is not None:bound['inputs']=obj({role:{'$ref':'#/$defs/InputRef'} for role in sorted(roles)},inputs.get('required_roles',[]))
                else:bound.pop('inputs',None);bound['input_bindings']=deepcopy(input_bindings);input_field='input_bindings'
                operation_required=[key for key in required if key!='acceptance']
                if contract.get('evidence_only'):operation_required.append('purpose')
                operation_required=[key for key in operation_required if key!='inputs']
                name='OperationCommandTask'+str(index);defs[name]=obj(bound,list(dict.fromkeys([*operation_required,'operation',input_field,'parameters'])))
                task_variants.append({'$ref':'#/$defs/'+name})
        else:
            name=kind.title()+'CommandTask';defs[name]=obj(fields,required);task_variants.append({'$ref':'#/$defs/'+name})
    patches=[]
    for index,(identifier,allowed) in enumerate(available_changes(frozen).items()):
        name='TaskChange'+str(index)
        patch_fields={key:deepcopy(props[key]) for key in allowed if key not in ('outputs','output_roles','inputs')}
        if 'inputs' in allowed:patch_fields['input_bindings']=deepcopy(input_bindings)
        if 'outputs' in allowed or 'output_roles' in allowed:patch_fields['files']=deepcopy(files)
        defs[name]=obj({'task_id':{'type':'string','enum':[identifier]},
                       'set':obj(patch_fields,[])})
        patches.append({'$ref':'#/$defs/'+name})
    patch_items={'anyOf':patches} if patches else obj({'task_id':{'type':'string'},'set':obj({})})
    proposal=domain['$defs']['Proposal']['properties']
    plan=obj({'new_tasks':{'type':'array','maxItems':64,'items':{'anyOf':task_variants}},
              'changes':{'type':'array','maxItems':16 if patches else 0,'items':patch_items},
              'dependencies':deepcopy(proposal['add_dependencies']),
              'remove_dependencies':deepcopy(proposal['remove_dependencies']),
              'replacements':deepcopy(proposal['replacements']),
              'review_again':deepcopy(proposal['review_again'])},[])
    eligible_reviews={item['task_id']:item['eligible_evidence_tasks'] for item in repair_options(frozen)
                      if item['action']=='review_again'}
    plan['properties']['review_again']=obj({identifier:{'type':'array','minItems':1,'maxItems':16,
        'uniqueItems':True,'items':{'type':'string','enum':evidence_ids}}
        for identifier,evidence_ids in sorted(eligible_reviews.items())},[])
    automatic,user=scopes(frozen)
    if automatic is None and user is None:
        plan['properties']['replacements']=obj({})
        plan['properties']['review_again']=obj({})
    if automatic is not None and user is None:
        # A replacement changes implementation, never asks the model to rewrite
        # the exact requirement text/criteria that must remain immutable.
        if not evidence_collection_available(frozen):plan['properties']['new_tasks']['maxItems']=0
        ids=[t['id'] for t in frozen.get('tasks',[]) if automatic in ('',t['id']) and t['status'] in ('paused','failed','succeeded')]
        replacement_variants=[]
        for variant in task_variants:
            original_name=variant['$ref'][8:];original=deepcopy(defs[original_name])
            for key in (*REPLACEMENT_REQUIREMENT_FIELDS,'id'):
                original['properties'].pop(key,None)
                original['required']=[x for x in original['required'] if x!=key]
            name=original_name+'ReplacementImplementation';defs[name]=original;replacement_variants.append({'$ref':'#/$defs/'+name})
        plan['properties']['replacements']=obj({})
        plan['properties']['repair_replacements']={'type':'array','maxItems':16 if ids else 0,'items':obj({
            'task_id':{'type':'string','enum':ids or ['__none__']},'implementation':{'anyOf':replacement_variants}}),
            'description':'Replace a failed logical Task with a new implementation. The host inherits its exact objective, acceptance, semantics and delivery stage; do not restate them.'}
    fields={key:deepcopy(value) for key,value in domain['properties'].items()
            if key not in ('proposal','snapshot_digest','base_graph_version','authorization_version','input_through_sequence')}
    fields['plan']={'anyOf':[plan,{'type':'null'}]}
    if frozen.get('scope_confirmation_required'):
        fields['kind']['enum']=['REQUEST_AUTHORIZATION','REQUEST_USER_INPUT','NEW_RUN_REQUIRED']
    if targeted_user_inputs(frozen):
        fields['kind']['enum']=[kind for kind in fields['kind']['enum'] if kind!='NEW_RUN_REQUIRED']
    result=obj(fields,['kind','reason']);result['$defs']=defs
    return result


def compile_manager(command,frozen):
    # Identity comes exclusively from this immutable request, never a fresh DB snapshot.
    result={key:value for key,value in command.items() if key!='plan'}
    if result.get('kind')=='NEW_RUN_REQUIRED' and targeted_user_inputs(frozen):
        raise CommandError('same_run_revision_required','kind',
            'The current user input targets existing Tasks. Revise only that authorized Task closure in this Run.')
    if result.get('kind')=='REQUEST_AUTHORIZATION' and result.get('authorization') is not None:
        result['authorization']={**result['authorization'],'goal':frozen['goal']}
    result.update(snapshot_digest=frozen['snapshot_digest'],base_graph_version=frozen['graph_version'],
                  authorization_version=frozen['authorization_version'],
                  input_through_sequence=(frozen.get('input_batch') or {}).get('through_sequence',0))
    plan=command.get('plan')
    if plan is not None:
        plan=deepcopy(plan)
        automatic,user=scopes(frozen);current={t['id']:t for t in frozen.get('tasks',[])}
        for item in plan.pop('repair_replacements',[]):
            identifier=item['task_id']
            if user is not None or automatic not in ('',identifier) or identifier not in current:
                raise CommandError('replacement_not_available','plan.repair_replacements','Replacement is outside the frozen automatic repair scope')
            implementation=deepcopy(item['implementation'])
            if any(key in implementation for key in REPLACEMENT_REQUIREMENT_FIELDS):
                raise CommandError('frozen_requirement','plan.repair_replacements.implementation','Requirement fields are inherited by the host')
            old=current[identifier]['spec']
            implementation['id']=(identifier[:24]+'_r_'+graph.digest({
                'request':frozen['planning_request_id'],'task':identifier})[:8])[:40]
            implementation.update({key:deepcopy(old[key]) for key in REPLACEMENT_REQUIREMENT_FIELDS})
            if identifier in plan.setdefault('replacements',{}):raise CommandError('duplicate_replacement','plan.repair_replacements','Replace each logical Task at most once')
            plan['replacements'][identifier]=implementation['id'];plan.setdefault('new_tasks',[]).append(implementation)
        replacement_sources={new:old for old,new in plan.get('replacements',{}).items()}
        added=[];replacement_ids=set(replacement_sources)
        for task in plan.get('new_tasks',[]):
            raw=deepcopy(task);files=raw.pop('files',[]);bindings=raw.pop('input_bindings',None);outputs=[];roles={}
            source=replacement_sources.get(raw.get('id'))
            if source in current:
                raw.update({key:deepcopy(current[source]['spec'][key]) for key in REPLACEMENT_REQUIREMENT_FIELDS})
            if raw.get('id') not in replacement_ids:
                raw['acceptance']=[{**check,'source':'user'} for check in raw.get('acceptance',[])]
            contract_filenames=[];role_filenames={}
            if bindings is not None:
                if len({item['role'] for item in bindings})!=len(bindings):raise CommandError('duplicate_input_role','plan.new_tasks.input_bindings','Declare each input role once')
                raw['inputs']={item['role']:item['ref'] for item in bindings}
            if raw['type']=='operation':
                binding=next((op for op in frozen.get('operations',[]) if op['id']==raw['operation']),None)
                if not binding:raise CommandError('operation_unavailable','plan.new_tasks.operation','Operation is outside the frozen capability index')
                raw['implementation']=binding['implementation']
                contract=(binding.get('output_contract') or {})
                role_filenames=contract.get('role_filenames',{})
                contract_filenames=[name for role,name in contract.get('role_filenames',{}).items() if role!='preview']
                if raw.get('id') in replacement_ids:
                    if not raw.get('basic_reason'):raw['basic_reason']='Server-owned registered operation integrity checks'
                    added.append(raw);continue
                business=[]
                for check in raw.get('acceptance',[]):
                    roles=list(binding.get('outputs',[]))
                    preferred=[]
                    if check.get('transparent_subject') is True:preferred=['foreground','primary_image','image']
                    elif check.get('kind')=='image':preferred=['primary_image','image','foreground','mask','preview']
                    elif check.get('kind')=='media':preferred=[]
                    elif check.get('kind') in ('text','subtitle'):preferred=['text','subtitle']
                    elif check.get('kind')=='json':preferred=['report','plan']
                    target='*' if raw.get('id') in replacement_ids else next(
                        (role for role in preferred if role in roles),roles[0] if len(roles)==1 else '*')
                    role_filenames=(binding.get('output_contract') or {}).get('role_filenames',{})
                    if target!='*':target=role_filenames.get(target,'*')
                    normalized={**check,'file':target,'source':'user'}
                    if normalized not in business:business.append(normalized)
                from .quality_policy import Acceptance
                technical=[Acceptance.model_validate(check).model_dump() for check in operation_acceptance(binding,raw.get('parameters',{}))]
                raw['acceptance']=[*business,*[check for check in technical if check not in business]]
                if binding.get('evidence_only'):
                    raw['purpose']='evidence';raw['delivery_stage']=False
                    raw['acceptance']=[{**check,'stage':'current'} for check in raw['acceptance']]
                if not raw.get('basic_reason'):
                    raw['basic_reason']='Server-owned registered operation integrity checks'
            for item in files:
                if item['path'] in outputs:raise CommandError('duplicate_output','plan.new_tasks.files','Declare each output path once')
                outputs.append(item['path'])
                for role in item['roles']:
                    if role in roles:raise CommandError('duplicate_output_role','plan.new_tasks.files.roles','Each role identifies exactly one file')
                    roles[role]=item['path']
            if task['type']!='operation':
                if 'report' in required_delivery_roles(frozen) and 'report' not in roles:
                    reports={path for role,path in roles.items() if role.endswith('_report')}
                    if len(reports)==1:roles['report']=reports.pop()
                    elif len(reports)>1:
                        raise CommandError('ambiguous_report_role','plan.new_tasks.files.roles',
                            'Assign the canonical report role to the requested report; multiple report roles are ambiguous.')
                bind_explicit_delivery_names(raw,outputs,roles,frozen)
                raw.update(outputs=outputs,output_roles=roles)
            bind_explicit_delivery_formats(raw,contract_filenames if task['type']=='operation' else outputs,frozen,
                                           role_filenames if task['type']=='operation' else roles)
            if 'delivery_stage' not in raw:
                raw['delivery_stage']=any(check.get('source','user')=='user' and check.get('stage')=='final' for check in raw.get('acceptance',[]))
            added.append(raw)
        current={t['id']:t for t in frozen.get('tasks',[])};allowed=available_changes(frozen);revisions=[];seen=set()
        for change in plan.get('changes',[]):
            identifier=change['task_id'];patch=deepcopy(change['set'])
            bindings=patch.pop('input_bindings',None)
            if bindings is not None:
                if len({item['role'] for item in bindings})!=len(bindings):raise CommandError('duplicate_input_role','plan.changes.set.input_bindings','Declare each input role once')
                patch['inputs']={item['role']:item['ref'] for item in bindings}
            files=patch.pop('files',None)
            if files is not None:
                outputs=[];roles={}
                for item in files:
                    if item['path'] in outputs:raise CommandError('duplicate_output','plan.changes.set.files','Declare each output path once')
                    outputs.append(item['path'])
                    for role in item['roles']:
                        if role in roles:raise CommandError('duplicate_output_role','plan.changes.set.files.roles','Each role identifies exactly one file')
                        roles[role]=item['path']
                bind_explicit_delivery_names(patch,outputs,roles,frozen)
                patch.update(outputs=outputs,output_roles=roles)
            if identifier in seen:raise CommandError('duplicate_change','plan.changes','Change each Task at most once')
            seen.add(identifier)
            if identifier not in allowed:raise CommandError('revision_not_available','plan.changes.task_id','Task is outside the frozen revision scope')
            invalid=set(patch)-set(allowed[identifier])
            if invalid:raise CommandError('frozen_field_change','plan.changes.set.'+sorted(invalid)[0],'This field is frozen; use an authorized replacement if its contract must change')
            revised={**deepcopy(current[identifier]['spec']),**patch}
            if revised['type']=='agent':
                outputs=list(revised.get('outputs',[]));roles=dict(revised.get('output_roles',{}))
                bind_explicit_delivery_names(revised,outputs,roles,frozen)
                revised.update(outputs=outputs,output_roles=roles)
            revisions.append(revised)
        dependencies=list(plan.get('dependencies',[]));edge_keys={(item['upstream'],item['downstream']) for item in dependencies}
        for task in added:
            for ref in task.get('inputs',{}).values():
                if ref.get('step') and (ref['step'],task['id']) not in edge_keys:
                    dependencies.append({'upstream':ref['step'],'downstream':task['id']});edge_keys.add((ref['step'],task['id']))
        replacements=dict(plan.get('replacements',{}))
        if replacements:
            for task in added:
                for ref in task.get('inputs',{}).values():
                    if ref.get('step') in replacements:ref['step']=replacements[ref['step']]
            dependencies=[{'upstream':replacements.get(edge['upstream'],edge['upstream']),
                           'downstream':replacements.get(edge['downstream'],edge['downstream'])}
                          for edge in dependencies]
            dependencies=list({(edge['upstream'],edge['downstream']):edge for edge in dependencies}.values())
        revised_ids={task['id'] for task in revisions}
        for index,task in enumerate(added):
            if task.get('purpose')!='evidence':continue
            for role,ref in task.get('inputs',{}).items():
                source=ref.get('step')
                if (source in current and current[source]['status']!='succeeded'
                        and source not in revised_ids and source not in replacements):
                    raise CommandError('evidence_candidate_required',
                        f'plan.new_tasks.{index}.inputs.{role}',
                        'Evidence for a paused or failed Task must use its frozen repair_candidate; a normal step input waits for accepted output and can never run')
        if automatic is not None and user is None:
            replacement_ids=set(replacements.values())
            invalid=[task['id'] for task in added if task['id'] not in replacement_ids and
                     not (task['type']=='operation' and task.get('purpose')=='evidence' and evidence_collection_available(frozen))]
            if invalid:
                raise CommandError('evidence_task_required' if evidence_collection_available(frozen) else 'new_task_not_available','plan.new_tasks',
                    'Automatic repair permits only explicit replacements or registered evidence Operations')
        changed={task['id'] for task in added}|revised_ids
        changed|={identifier for identifier in plan.get('review_again',{}) if identifier in current}
        changed={replacements.get(identifier,identifier) for identifier in changed}
        removed={(replacements.get(edge['upstream'],edge['upstream']),replacements.get(edge['downstream'],edge['downstream']))
                 for edge in plan.get('remove_dependencies',[])}
        effective={(replacements.get(edge['upstream_task_id'],edge['upstream_task_id']),
                    replacements.get(edge['downstream_task_id'],edge['downstream_task_id']))
                   for edge in frozen.get('dependencies',[])}-removed
        effective|={(edge['upstream'],edge['downstream']) for edge in dependencies}
        affected=set(changed)
        while True:
            downstream={target for source,target in effective if source in affected}
            expanded=affected|downstream
            if expanded==affected:break
            affected=expanded
        waiting=sorted(identifier for identifier in affected
                       if not any(source==identifier and target in affected for source,target in effective))
        if result['kind']=='APPLY_PLAN' and not waiting:
            eligible=[item for item in repair_options(frozen) if item['action'] in ('review_again','collect_evidence_then_review_again')]
            if eligible:
                advice='Use plan.review_again with the completed eligible evidence Task IDs; do not submit an empty APPLY_PLAN.'
            elif (frozen.get('input_batch') or {}).get('through_sequence',0):
                advice='Use continue_run when the frozen input batch requires no graph change.'
            else:
                advice='Add a new, revised, replaced, or review_again Task, or use an available explicit control outcome.'
            raise CommandError('missing_wait_work','plan','APPLY_PLAN requires work that can execute or review. '+advice)
        result['proposal']={'proposal_id':frozen['planning_request_id'],'base_graph_version':frozen['graph_version'],
            'reason':command['reason'],'add_tasks':added,'revise_tasks':revisions,
            'add_dependencies':dependencies,'remove_dependencies':plan.get('remove_dependencies',[]),
            'replacements':replacements,'review_again':plan.get('review_again',{}),
            'sealed':result['kind']=='COMPLETE','replan_after':waiting if result['kind']=='APPLY_PLAN' else []}
    return graph.ManagerResult.model_validate(result).model_dump()


class ToolProtocol:
    def __init__(self,profile,frozen,task,*,finalize=False,inspections=None,candidate_keys=None,contracts=None,draft=None,store=None):
        self.profile=profile;self.frozen=deepcopy(frozen);self.task=task;self.store=store;self._draft=draft;self.contracts=contracts or {};self.schemas={};self.names={}
        self.correction_mode=bool(draft and draft.get('last_error_signature'))
        tools=set(profile.tools)&({'proposal','result'} if finalize else set(profile.tools))
        if 'inspect' in tools:
            refs=frozen.get('inspectable_refs',[])
            if refs:self.add('inspect','inspect',obj({'refs':{'type':'array','minItems':1,'maxItems':8,'items':obj({
                'ref':{'type':'string','enum':refs},'offset':{'type':'number','minimum':0},
                'window_seconds':{'type':'number','exclusiveMinimum':0,'maximum':8}},['ref'])}}))
        if 'contract' in tools:
            operations=[row['id'] for row in frozen.get('operations',[])]
            if operations:self.add('contract','contract',obj({'operations':{'type':'array','minItems':1,'maxItems':8,
                'uniqueItems':True,'items':{'type':'string','enum':operations}}}))
        if 'script' in tools:self.add('script','script',obj({'code':{'type':'string','minLength':1,'maxLength':64000}}))
        if 'proposal' in tools:
            if draft is None:self.add('submit_plan','proposal',manager_schema(frozen,contracts))
            else:
                self.add_draft_tools(draft,contracts or {},finalize)
                if completion_candidate(frozen):
                    reason=manager_schema(frozen,contracts,allow_script=False)['properties']['reason']
                    self.add('complete_run','draft_control',obj({'reason':reason},['reason']))
        if 'result' in tools:
            source=deepcopy(frozen.get('result_schema') or {})
            if task.get('control_kind')=='REVIEW':
                source['properties'].pop('snapshot_digest',None);source['required']=[x for x in source.get('required',[]) if x!='snapshot_digest']
                identifiers=list(inspections or {})
                from .workflow_agent_contracts import review_candidate_aliases
                selections=list(review_candidate_aliases(frozen,inspections or {},candidate_keys or {}))
                if 'Evidence' in source.get('$defs',{}):
                    source['$defs']['Evidence']['properties']['inspection_id']={'type':'string','enum':identifiers or ['__no_completed_inspections__']}
                source['properties']['deliverables']['items']={'type':'string','enum':selections or ['__no_eligible_delivery_candidates__']}
                source['properties']['primary']={'type':'string','enum':['',*selections]}
                if frozen.get('review_scope')=='task':source['properties']['deliverables']['maxItems']=0;source['properties']['primary']['enum']=['']
            if not source:
                source=obj({'files':obj({name:{'type':'string'} for name in task['spec'].get('outputs',[])},[])})
            self.add('submit_result','result',source)
        if not self.schemas:raise ValueError('No available actions in the frozen profile phase')

    def add(self,name,action,source):self.schemas[name]=source;self.names[name]=action

    def execution_actions(self):
        """Actions emitted by decode(), including host-compiled draft commits."""
        return {'proposal' if action in ('draft_commit','draft_control') else action for action in self.names.values()}

    def add_draft_tools(self,draft,contracts,finalize):
        command=manager_schema(self.frozen,contracts,allow_script=False);defs=command['$defs'];plan=command['properties']['plan']['anyOf'][0]['properties']
        def referenced(item):return referenced_schema(obj({'tasks':{'type':'array','minItems':1,'maxItems':16,'items':item}}),defs)
        draft_value=draft.get('draft',draft);existing=draft_value.get('new_tasks',[])
        can_correct=self.correction_mode
        config_fields={key:deepcopy(plan[key]) for key in ('changes','dependencies','remove_dependencies','repair_replacements','replacements','review_again') if key in plan}
        commit_fields={'kind':{'type':'string','enum':['APPLY_PLAN','COMPLETE']},'reason':command['properties']['reason']}
        automatic,user=scopes(self.frozen);targeted=targeted_user_inputs(self.frozen)
        evidence_repair=automatic is not None and user is None and evidence_collection_available(self.frozen)
        allow_new_tasks=((automatic is None or user is not None) and not targeted) or evidence_repair
        task_variants=[]
        if 'AgentCommandTask' in defs and not evidence_repair:
            schema=referenced({'$ref':'#/$defs/AgentCommandTask'})
            task_variants.append({'$ref':'#/$defs/AgentCommandTask'})
            if can_correct and any(item.get('type')=='agent' for item in existing):self.add('replace_agent_tasks','draft_replace_tasks',schema)
        for index,identifier in enumerate(sorted(contracts)):
            if evidence_repair and not contracts[identifier].get('evidence_only'):continue
            name='OperationCommandTask'+str(index)
            if name in defs:
                schema=referenced({'$ref':'#/$defs/'+name})
                task_variants.append({'$ref':'#/$defs/'+name})
                if can_correct and any(item.get('type')=='operation' and item.get('operation')==identifier for item in existing):
                    self.add('replace_operation_tasks_'+str(index),'draft_replace_tasks',schema)
        # An empty Draft has nothing that local replace/remove tools can repair.
        # Keep the bounded typed-plan entry available even in finalize mode so a
        # rejected first submission can be corrected instead of forcing input.
        if not existing or not finalize and not can_correct:
            submit={**config_fields,**commit_fields}
            if task_variants and allow_new_tasks:submit['tasks']={'type':'array','minItems':1,'maxItems':16,'items':{'anyOf':task_variants}}
            self.add('submit_typed_plan','draft_batch_commit',referenced_schema(obj(submit,['kind','reason']),defs))
        if config_fields:
            schema=referenced_schema(obj(config_fields,[]),defs)
            if can_correct and any(draft_value.get(key) for key in config_fields):self.add('replace_plan_configuration','draft_replace_configuration',schema)
        identifiers=[item['id'] for item in existing]
        if can_correct and identifiers:self.add('remove_draft_tasks','draft_remove_tasks',obj({'task_ids':{'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,'items':{'type':'string','enum':identifiers}}}))
        if existing or finalize or can_correct:self.add('commit_plan','draft_commit',obj(commit_fields))
        if (self.frozen.get('input_batch') or {}).get('through_sequence',0):
            self.add('continue_run','draft_control',obj({'reason':command['properties']['reason']}))
        self.add('request_user_input','draft_control',obj({'reason':command['properties']['reason'],
            'question':command['properties']['question'],'options':command['properties']['options']},['reason','question']))
        defs['AuthorizationExpansion']=deepcopy(defs['Authorization']);auth=defs['AuthorizationExpansion']
        auth['properties'].pop('goal',None);auth['required']=[key for key in auth.get('required',[]) if key!='goal']
        authorization=deepcopy(command['properties']['authorization'])
        def expansion_ref(value):
            if isinstance(value,dict):return {key:('#/$defs/AuthorizationExpansion' if key=='$ref' and item=='#/$defs/Authorization' else expansion_ref(item)) for key,item in value.items()}
            if isinstance(value,list):return [expansion_ref(item) for item in value]
            return value
        authorization=expansion_ref(authorization)
        self.add('request_authorization','draft_control',referenced_schema(obj({'reason':command['properties']['reason'],
            'authorization':authorization}),defs))
        if not targeted:
            self.add('new_run_required','draft_control',referenced_schema(obj({'reason':command['properties']['reason'],
                'new_goal':command['properties']['new_goal'],'authorization':command['properties']['authorization']}),defs))

    def definitions(self):
        descriptions={'inspect':'Inspect authorized immutable inputs. Returns evidence IDs.',
            'contract':'Read up to eight frozen operation contracts in one call; this does not authorize execution.',
            'script':'Execute a bounded local implementation in the Task output directory.',
            'submit_plan':'Submit a plan command. Identity and unchanged Task fields are bound by the host.',
            'submit_result':'Submit the declared result. Existing evidence and domain rules remain required.',
            'submit_typed_plan':'Atomically submit one bounded typed plan. The host derives continuation, dependencies and immutable fields.',
            'replace_script_tasks':'Replace invalid script Tasks in the server-owned draft without rewriting the plan.',
            'replace_agent_tasks':'Replace invalid specialist Tasks in the server-owned draft without rewriting the plan.',
            'replace_plan_configuration':'Replace invalid dependency or revision sections in the server-owned draft.',
            'remove_draft_tasks':'Remove invalid new Tasks and their draft dependencies.',
            'commit_plan':'Validate and atomically submit the current server-owned draft.',
            'complete_run':'Seal only when the entire user goal and all requested deliverables are fulfilled, for independent final review. A successful partial batch may still need new Tasks. No Task or dependency changes are accepted by this tool.',
            'continue_run':'Finish this planning round without changing the graph.',
            'request_user_input':'Request information that is required before planning can continue.',
            'request_authorization':'Request a complete expanded authorization.',
            'new_run_required':'Request a successor Run for a changed top-level goal.'}
        result=[{'name':name,'description':descriptions.get(name,'Append Tasks for one queried Operation to the server-owned draft.'),
                 'schema':schema_for(source)} for name,source in self.schemas.items()]
        metrics=schema_complexity(result)
        if (metrics['schema_bytes']>512000 or metrics['schema_max_depth']>80 or
                metrics['schema_object_count']>2500 or metrics['schema_union_branches']>1500):
            raise ValueError('Model command schema exceeds the complexity budget')
        return result

    def decode(self,name,arguments):
        if name not in self.schemas:raise CommandError('action_unavailable','tool','Tool is unavailable in this role or phase')
        dynamic={identifier for identifier,contract in self.contracts.items() if contract.get('input_contract',{}).get('allowed_roles') is None}
        arguments,normalizations=normalize_planner_arguments(name,arguments,dynamic)
        payload=transform(arguments,self.schemas[name]);action=self.names[name]
        if action=='proposal':return {'action':'proposal','proposal':compile_manager(payload,self.frozen),'_model_command':payload}
        if action in ('draft_add_tasks','draft_replace_tasks','draft_remove_tasks','draft_configure','draft_replace_configuration'):
            if action in ('draft_add_tasks','draft_batch_commit'):
                existing={item['id'] for item in self.frozen.get('tasks',[])}
                duplicate=next((item['id'] for item in payload.get('tasks',[]) if item['id'] in existing),None)
                if duplicate:raise CommandError('existing_task_id','tasks.id','New draft Tasks cannot reuse an existing logical Task ID; use the declared repair/revision command')
            return {'action':action,'command':payload,'_model_command':payload}
        if action=='draft_batch_commit':return {'action':action,'command':payload,'_model_command':payload,'_normalizations':normalizations}
        if action=='draft_commit':
            from .workflow_plan_draft import compile as compile_draft,compile_value
            if self.store is None:
                value=self._draft.get('draft',self._draft);proposal=compile_value(value,self.frozen,payload)
                revision=self._draft.get('revision',0);draft_digest=graph.digest(value)
            else:proposal,revision,draft_digest=compile_draft(self.store,self.frozen['planning_request_id'],self.frozen,payload)
            return {'action':'proposal','proposal':proposal,'_model_command':payload,'_draft_commit':True,
                    '_draft_revision':revision,'_draft_digest':draft_digest}
        if action=='draft_control':
            kinds={'continue_run':'CONTINUE','request_user_input':'REQUEST_USER_INPUT',
                   'request_authorization':'REQUEST_AUTHORIZATION','new_run_required':'NEW_RUN_REQUIRED',
                   'complete_run':'COMPLETE'}
            value=self._draft.get('draft',self._draft)
            command={'kind':kinds[name],**payload}
            if name=='complete_run':command['plan']={}
            return {'action':'proposal','proposal':compile_manager(command,self.frozen),
                    '_model_command':payload,'_draft_commit':True,
                    '_draft_revision':self._draft.get('revision',0),'_draft_digest':graph.digest(value)}
        if action=='result':
            if self.task.get('control_kind')=='REVIEW':return {'action':'result','decision':{**payload,'snapshot_digest':self.frozen['snapshot_digest']}}
            if self.task.get('control_kind')=='DIAGNOSE':return {'action':'result','diagnosis':payload,'snapshot_digest':self.frozen['snapshot_digest']}
            if self.profile.output_schema in ('visual-report-v1','diagnosis-v1'):return {'action':'result','report':payload}
        return {'action':action,**payload}

    def context(self):
        context=deepcopy(self.frozen);context.pop('proposal_schema',None);context.pop('result_schema',None)
        context.pop('result_contract',None)
        context['model_command_contract']={'version':'native-commands-v1','available_tools':list(self.schemas),
            'task_changes':available_changes(self.frozen) if 'submit_plan' in self.schemas else {},
            'identity':'Host binds the original immutable request; do not copy snapshot/version fields.',
            'maps':'Dynamic maps are key/value entry arrays as declared in the tool schema; keys must be unique.',
            'host_derived_fields':['task_id_for_replacements','dependencies_from_inputs','revision','technical_acceptance','delivery_stage','replan_after'],
            'planning':'Use kind COMPLETE to seal a finished graph or APPLY_PLAN when this batch adds executable work. The host derives wait leaves from the changed dependency graph. Task changes contain only allowed set fields.'}
        if 'submit_typed_plan' in self.schemas or 'commit_plan' in self.schemas:
            from .workflow_plan_draft import summary
            context['model_command_contract'].update(version='plan-draft-v1',planning=
                'Use submit_typed_plan once with the state-allowed typed changes. The host derives continuation and atomically validates a server-owned draft. If validation reports a specific error, use only the offered replace/remove tool and then commit_plan. Draft commands never execute business work.',
                draft_state=summary(self._draft.get('draft',self._draft)))
        if 'complete_run' in self.schemas:
            context['model_command_contract']['planning']+=' All declared Tasks succeeded, which does not prove the entire user goal is complete. Add any remaining authorized work with submit_typed_plan; use complete_run only when all requested deliverables are fulfilled.'
        if 'submit_typed_plan' in self.schemas:
            context['model_command_contract']['repair_options']=repair_options(self.frozen)
            delivery=[item['id'] for item in self.frozen.get('tasks',[]) if item['status'] not in ('failed','cancelled') and item['spec'].get('delivery_stage')]
            context['model_command_contract']['completion_requirements']={'current_delivery_stage_tasks':delivery,
                'complete_requires_delivery_stage':True,
                'instruction':'If current_delivery_stage_tasks is empty, COMPLETE must give the real producing Task a user acceptance with stage=final; the host derives delivery_stage. Never add a pass-through Agent that only copies an accepted upstream result. Reviewer and diagnostic reports are evidence, not delivery inputs.'}
        context['model_command_contract']['operations']='Query contract before adding an operation Task. Its exact typed Task variant then becomes available inside submit_typed_plan; the host binds the frozen implementation.'
        if 'submit_plan' in self.schemas:
            automatic,user=scopes(self.frozen)
            context['model_command_contract']['replacement']='Automatic repair uses plan.repair_replacements: choose an implementation; host inherits the original requirement exactly.' if automatic is not None and user is None else 'Authorized user feedback may use plan.replacements with explicitly declared new_tasks.'
        return context
