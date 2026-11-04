"""Model-facing commands compiled into the unchanged, fenced domain contracts."""
from copy import deepcopy
from . import workflow_graph as graph
from .strict_schema import obj,schema_for,transform


class CommandError(ValueError):
    def __init__(self,code,path,message):
        self.code=code;self.path=path
        super().__init__(message)


def scopes(frozen):
    repair=frozen.get('repair_context') or {};trigger=frozen.get('trigger') or {}
    automatic=(repair.get('subject_task','') if repair.get('active') else trigger.get('task_id')
               if 'task_id' in trigger else '' if any(k in trigger for k in ('review_rejected','task_review_rejected')) else None)
    records=(frozen.get('input_batch') or {}).get('inputs',[])
    user=set().union(*(set(item['context']['allowed_revision_tasks']) for item in records)) if records else None
    return automatic,user


def change_fields(task_type,user=False):
    return graph.revision_mutable_fields(task_type,user)


REPLACEMENT_REQUIREMENT_FIELDS=('objective','acceptance','semantics','delivery_stage')


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


def manager_schema(frozen,contracts=None):
    contracts=contracts or {}
    domain=graph.ManagerResult.model_json_schema();defs=deepcopy(domain['$defs']);task=defs['Task'];props=task['properties']
    from .quality_policy import Acceptance
    acceptance=defs['Acceptance']['properties'];variants=[]
    for kind in ('file','image','media','text','json','subtitle','agent','review'):
        fields={key:deepcopy(acceptance[key]) for key in sorted(Acceptance.fields_for_kind(kind))}
        fields['kind']={'type':'string','enum':[kind]};required=['kind']
        if kind in ('agent','review'):
            fields['severity']={'type':'string','enum':['required']};fields['criterion']['minLength']=1;required.append('criterion')
        if kind=='text':fields['contains']['minItems']=1;required.append('contains')
        name='CommandAcceptance'+kind.title();defs[name]=obj(fields,required);variants.append({'$ref':'#/$defs/'+name})
    defs['Acceptance']={'anyOf':variants}
    files={'type':'array','minItems':1,'maxItems':64,'items':obj({'path':{'type':'string','minLength':1,'maxLength':240},
           'roles':{'type':'array','minItems':1,'items':{'type':'string','minLength':1},'maxItems':32}}),
           'description':'Declare each local output file once and its roles. The server derives outputs/output_roles.'}
    task_variants=[]
    irrelevant={'script':{'operation','implementation','parameters','profile'},
                'operation':{'argv','outputs','output_roles','profile','preflight','config_files'},
                'agent':{'argv','operation','implementation','parameters','preflight','config_files'}}
    for kind,removed in irrelevant.items():
        fields={key:deepcopy(value) for key,value in props.items() if key not in removed|{'outputs','output_roles'}}
        fields['type']={'type':'string','enum':[kind]}
        if kind!='operation':fields['files']=files
        required=[key for key in task.get('required',[]) if key in fields]
        if kind!='operation':required.append('files')
        if kind=='operation':
            for index,(identifier,contract) in enumerate(sorted(contracts.items())):
                bound=deepcopy(fields);bound['operation']={'type':'string','enum':[identifier]};bound.pop('implementation',None)
                parameters=deepcopy(contract['parameters_schema']);prefix='Operation'+str(index)+'_'
                def rename(value):
                    if isinstance(value,dict):return {key:('#/$defs/'+prefix+item[8:] if key=='$ref' and isinstance(item,str) and item.startswith('#/$defs/') else rename(item)) for key,item in value.items()}
                    if isinstance(value,list):return [rename(x) for x in value]
                    return value
                for key,value in parameters.pop('$defs',{}).items():defs[prefix+key]=rename(value)
                bound['parameters']=rename(parameters)
                inputs=contract['input_contract'];roles=inputs.get('allowed_roles')
                if roles is not None:bound['inputs']=obj({role:{'$ref':'#/$defs/InputRef'} for role in sorted(roles)},inputs.get('required_roles',[]))
                name='OperationCommandTask'+str(index);defs[name]=obj(bound,list(dict.fromkeys([*required,'operation','inputs','parameters'])))
                task_variants.append({'$ref':'#/$defs/'+name})
        else:
            name=kind.title()+'CommandTask';defs[name]=obj(fields,required);task_variants.append({'$ref':'#/$defs/'+name})
    patches=[]
    for index,(identifier,allowed) in enumerate(available_changes(frozen).items()):
        name='TaskChange'+str(index)
        defs[name]=obj({'task_id':{'type':'string','enum':[identifier]},
                       'set':obj({key:deepcopy(props[key]) for key in allowed},[])})
        patches.append({'$ref':'#/$defs/'+name})
    patch_items={'anyOf':patches} if patches else obj({'task_id':{'type':'string'},'set':obj({})})
    continuation={'anyOf':[obj({'mode':{'type':'string','enum':['complete']}}),
        obj({'mode':{'type':'string','enum':['wait']},'tasks':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'string'}}})]}
    proposal=domain['$defs']['Proposal']['properties']
    plan=obj({'new_tasks':{'type':'array','maxItems':64,'items':{'anyOf':task_variants}},
              'changes':{'type':'array','maxItems':16 if patches else 0,'items':patch_items},
              'dependencies':deepcopy(proposal['add_dependencies']),
              'remove_dependencies':deepcopy(proposal['remove_dependencies']),
              'replacements':deepcopy(proposal['replacements']),
              'review_again':deepcopy(proposal['review_again']),
              'continuation':continuation},['continuation'])
    automatic,user=scopes(frozen)
    if automatic is None and user is None:
        plan['properties']['replacements']=obj({})
        plan['properties']['review_again']=obj({})
    if automatic is not None and user is None:
        # A replacement changes implementation, never asks the model to rewrite
        # the exact requirement text/criteria that must remain immutable.
        ids=[t['id'] for t in frozen.get('tasks',[]) if automatic in ('',t['id']) and t['status'] in ('paused','failed','succeeded')]
        replacement_variants=[]
        for variant in task_variants:
            original_name=variant['$ref'][8:];original=deepcopy(defs[original_name])
            for key in REPLACEMENT_REQUIREMENT_FIELDS:
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
    result=obj(fields,['kind','reason']);result['$defs']=defs
    return result


def compile_manager(command,frozen):
    # Identity comes exclusively from this immutable request, never a fresh DB snapshot.
    result={key:value for key,value in command.items() if key!='plan'}
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
            implementation.update({key:deepcopy(old[key]) for key in REPLACEMENT_REQUIREMENT_FIELDS})
            if identifier in plan.setdefault('replacements',{}):raise CommandError('duplicate_replacement','plan.repair_replacements','Replace each logical Task at most once')
            plan['replacements'][identifier]=implementation['id'];plan.setdefault('new_tasks',[]).append(implementation)
        added=[]
        for task in plan.get('new_tasks',[]):
            raw=deepcopy(task);files=raw.pop('files',[]);outputs=[];roles={}
            if raw['type']=='operation':
                binding=next((op for op in frozen.get('operations',[]) if op['id']==raw['operation']),None)
                if not binding:raise CommandError('operation_unavailable','plan.new_tasks.operation','Operation is outside the frozen capability index')
                raw['implementation']=binding['implementation']
            for item in files:
                if item['path'] in outputs:raise CommandError('duplicate_output','plan.new_tasks.files','Declare each output path once')
                outputs.append(item['path'])
                for role in item['roles']:
                    if role in roles:raise CommandError('duplicate_output_role','plan.new_tasks.files.roles','Each role identifies exactly one file')
                    roles[role]=item['path']
            if task['type']!='operation':raw.update(outputs=outputs,output_roles=roles)
            added.append(raw)
        current={t['id']:t for t in frozen.get('tasks',[])};allowed=available_changes(frozen);revisions=[];seen=set()
        for change in plan.get('changes',[]):
            identifier=change['task_id'];patch=change['set']
            if identifier in seen:raise CommandError('duplicate_change','plan.changes','Change each Task at most once')
            seen.add(identifier)
            if identifier not in allowed:raise CommandError('revision_not_available','plan.changes.task_id','Task is outside the frozen revision scope')
            invalid=set(patch)-set(allowed[identifier])
            if invalid:raise CommandError('frozen_field_change','plan.changes.set.'+sorted(invalid)[0],'This field is frozen; use an authorized replacement if its contract must change')
            revisions.append({**deepcopy(current[identifier]['spec']),**patch})
        continuation=plan['continuation']
        result['proposal']={'proposal_id':frozen['planning_request_id'],'base_graph_version':frozen['graph_version'],
            'reason':command['reason'],'add_tasks':added,'revise_tasks':revisions,
            'add_dependencies':plan.get('dependencies',[]),'remove_dependencies':plan.get('remove_dependencies',[]),
            'replacements':plan.get('replacements',{}),'review_again':plan.get('review_again',{}),
            'sealed':continuation['mode']=='complete','replan_after':continuation.get('tasks',[])}
    return graph.ManagerResult.model_validate(result).model_dump()


class ToolProtocol:
    def __init__(self,profile,frozen,task,*,finalize=False,inspections=None,candidate_keys=None,contracts=None):
        self.profile=profile;self.frozen=deepcopy(frozen);self.task=task;self.schemas={};self.names={}
        tools=set(profile.tools)&({'proposal','result'} if finalize else set(profile.tools))
        if 'inspect' in tools:
            refs=frozen.get('inspectable_refs',[])
            if refs:self.add('inspect','inspect',obj({'refs':{'type':'array','minItems':1,'maxItems':8,'items':obj({
                'ref':{'type':'string','enum':refs},'offset':{'type':'number','minimum':0},
                'window_seconds':{'type':'number','exclusiveMinimum':0,'maximum':8}},['ref'])}}))
        if 'contract' in tools:
            operations=[row['id'] for row in frozen.get('operations',[])]
            if operations:self.add('contract','contract',obj({'operation':{'type':'string','enum':operations}}))
        if 'script' in tools:self.add('script','script',obj({'code':{'type':'string','minLength':1,'maxLength':64000}}))
        if 'proposal' in tools:self.add('submit_plan','proposal',manager_schema(frozen,contracts))
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

    def definitions(self):
        return [{'name':name,'description':{'inspect':'Inspect authorized immutable inputs. Returns evidence IDs.',
            'contract':'Read a frozen operation contract; does not authorize it.',
            'script':'Execute a bounded local implementation in the Task output directory.',
            'submit_plan':'Submit a plan command. Identity and unchanged Task fields are bound by the host.',
            'submit_result':'Submit the declared result. Existing evidence and domain rules remain required.'}[name],
            'schema':schema_for(source)} for name,source in self.schemas.items()]

    def decode(self,name,arguments):
        if name not in self.schemas:raise CommandError('action_unavailable','tool','Tool is unavailable in this role or phase')
        payload=transform(arguments,self.schemas[name]);action=self.names[name]
        if action=='proposal':return {'action':'proposal','proposal':compile_manager(payload,self.frozen),'_model_command':payload}
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
            'planning':'Use plan.continuation.mode=complete OR wait with tasks. Task changes contain only allowed set fields.'}
        context['model_command_contract']['operations']='Query contract before adding an operation Task. Its exact input roles and parameter schema then become available in submit_plan; the host binds the frozen implementation.'
        if 'submit_plan' in self.schemas:
            automatic,user=scopes(self.frozen)
            context['model_command_contract']['replacement']='Automatic repair uses plan.repair_replacements: choose an implementation; host inherits the original requirement exactly.' if automatic is not None and user is None else 'Authorized user feedback may use plan.replacements with explicitly declared new_tasks.'
        return context
