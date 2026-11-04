"""Paired real-model proposal evaluation; reads frozen cases, never executes business work."""
from __future__ import annotations
import argparse,collections,concurrent.futures,copy,dataclasses,hashlib,json,os,sqlite3,sys,time
from pathlib import Path
ROOT=Path(os.environ.get('MODEL_EVAL_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT))
from webapp import workflow_graph as graph
from webapp.workflow_model_protocol import ToolProtocol,scopes
from webapp.strict_schema import schema_for,transform
from webapp.specialist_profiles import get as policy
from webapp.workflow_agents import model_instructions,decode_reply
from webapp.model_adapters import get as adapter_for
from webapp.model_tool_protocol import request as tool_request,response as tool_response,strict_unsupported
from webapp.egress_policy import session,assert_no_credentials

EVAL=Path(os.environ.get('MODEL_EVAL_ROOT',ROOT/'.cluster/model-evals/reply-v1'))

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o600);temp.replace(path)


def mode(f):
    return ('authorization' if f.get('scope_confirmation_required') else 'feedback' if (f.get('input_batch') or {}).get('inputs')
            else 'repair' if f.get('repair_context',{}).get('active') else 'initial' if not f.get('tasks') else 'continuation')


def build():
    if (EVAL/'dataset.json').exists():raise ValueError('Dataset already frozen; choose a new evaluation directory')
    from deploy import dynamic_cluster as cluster
    os.environ.update(cluster.environment())
    from webapp.store import Store
    from webapp.workflow_planning import available_operations
    from webapp.operations import get as operation
    from webapp.provider_metadata import metadata
    store=Store(cluster.data('node','a'),initialize_schema=False)
    catalog={op['id']:op for op in available_operations(store)}
    pools=collections.defaultdict(list)
    cases=['S01','S05','S14','D30','D31','D33','D36','D38','D39','D40','S02','S03','S04','S06','S07','S11','S12','D20','D24','D28','D41']
    for case in cases:
        paths=sorted((ROOT/'.cluster/campaigns/dynamic-plan/runs'/case).glob('attempt-*/internal-final.json'))
        if not paths:continue
        source=paths[-1];audit=json.loads(source.read_text())
        for run in audit['database']['runs']:
            for req in run['audit']['planner_snapshots']:
                f=json.loads(req['snapshot_json'])
                if any(t['status'] in ('running','leased','reviewing','waiting') for t in f['tasks']):continue
                if any(v.get('used',0)>0 for v in f.get('budget_state',{}).get('provider_calls',{}).values()):continue
                # Adapt archived policies to this isolated evaluator, identically for every arm.
                f['snapshot_digest']=graph.digest(f)
                auth=f['authorization'];auth['agent_profile_digests']={name:graph.digest(policy(name).snapshot()) for name in auth['allowed_agent_profiles']}
                for item in f.get('profiles',[]):
                    if item['id'] in auth['agent_profile_digests']:item['harness']=policy(item['id']).harness
                previous=json.loads(req['proposal_json'] or '{}').get('proposal') or {}
                related={t.get('operation') for t in previous.get('add_tasks',[])+previous.get('revise_tasks',[])}
                related.update(t['spec'].get('operation') for t in f['tasks'])
                related.update(identifier for identifier in catalog if identifier in f['goal'])
                contracts={name:catalog[name] for name in sorted(x for x in related if x in catalog)}
                f['operations']=[{**old,'implementation':contracts[old['id']]['implementation'],'contract_digest':graph.digest(contracts[old['id']])}
                                 for old in f.get('operations',[]) if old['id'] in contracts]
                for name,contract in contracts.items():
                    if name in auth['operation_implementations']:
                        auth['operation_implementations'][name]=contract['implementation']
                        auth['operation_policy_digests'][name]=graph.digest(metadata(operation(name,contract['implementation'])))
                f['provided_operation_contracts']=contracts
                f['snapshot_digest']=graph.digest({key:value for key,value in f.items() if key!='snapshot_digest'})
                pools[mode(f)].append({'source_case':case,'source_file':str(source.relative_to(ROOT)),
                    'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'request_id':req['id'],
                    'frozen':f,'archive_run':run,'normalization':'Same immutable input facts and complete related operation contracts for all arms. Policy/capability digests rebound to evaluator version; absent persistence-only counters normalized to zero. Finalize-only proposal evaluation, not full transaction replay or tool-discovery evaluation.'})
    quotas={'initial':10,'continuation':9,'repair':6,'feedback':4,'authorization':1};selected=[]
    for kind,count in quotas.items():
        if len(pools[kind])<count:raise ValueError(f'Insufficient {kind} samples: {len(pools[kind])}/{count}')
        for index,item in enumerate(pools[kind][:count]):
            item.update(id=f'{kind}-{index+1:02d}',mode=kind,split='development' if index%3==0 else 'held_out');selected.append(item)
    # Any request exposed during an earlier development pilot cannot be held out.
    exposed=set()
    for prior in EVAL.parent.glob('*/dataset.json'):
        if prior.parent==EVAL:continue
        old=json.loads(prior.read_text());lookup={c['id']:c['request_id'] for c in old['cases']}
        for result in (prior.parent/'results').glob('*/*/*/*/result.json'):
            exposed.add(lookup[json.loads(result.read_text())['case']])
    development={item['id'] for item in selected if item['request_id'] in exposed}
    for item in selected:
        if len(development)>=12:break
        if item['split']=='development':development.add(item['id'])
    for item in selected:item['split']='development' if item['id'] in development else 'held_out'
    save(EVAL/'dataset.json',{'created_at':time.time(),'cases':selected,'total':len(selected),'business_execution':False})
    print(json.dumps({'cases':len(selected),'modes':quotas,'splits':dict(collections.Counter(x['split'] for x in selected))}))


def rebind():
    if (EVAL/'dataset.json').exists():raise ValueError('Dataset already frozen; choose a new evaluation directory')
    source=Path(os.environ['MODEL_EVAL_SOURCE_DATASET']);dataset=json.loads(source.read_text())
    from tests.campaign_model_fixture import rebind as bind,validate
    rebound=bind(dataset,lambda name:graph.digest(policy(name).snapshot()))
    rebound['rebound_at']=time.time();rebound['rebound_from']=str(source)
    validate(rebound,lambda name:graph.digest(policy(name).snapshot()))
    save(EVAL/'dataset.json',rebound)
    print(json.dumps({'cases':len(rebound['cases']),'source':str(source),'facts_preserved':True}))


class EvaluationEvidenceMissing(ValueError):pass


class FrozenStore:
    """Read-only in-memory projection of archived authoritative snapshot facts.

    Missing persistence-only budgets are explicit fixture defaults; this evaluates
    proposal legality, not execution, model media judgments, or transaction races.
    """
    def __init__(self,case):
        f=case['frozen'];archive=case['archive_run'];self.f=f;self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.artifacts=self
        self.db.executescript('''CREATE TABLE execution_leases(attempt TEXT,active INT,expires REAL);
        CREATE TABLE execution_cleanup(attempt TEXT,state TEXT);
        CREATE TABLE workflow_reviews(id TEXT,run_id TEXT,subject_task TEXT,status TEXT);
        CREATE TABLE workflow_provider_calls(workflow TEXT,step TEXT,revision INT,implementation TEXT,state TEXT,request_key TEXT);
        CREATE TABLE run_authorizations(run_id TEXT,version INT,authorization_json TEXT);
        CREATE TABLE workflow_task_revisions(run_id TEXT,task_id TEXT,revision INT,spec_json TEXT,created_graph_version INT);
        CREATE TABLE workflow_plan_tasks(run_id TEXT,graph_version INT,task_id TEXT,task_revision INT);
        CREATE TABLE execution_step_checkpoints(workflow TEXT,step TEXT,revision INT,project TEXT,manifest TEXT);
        CREATE TABLE task_results(run_id TEXT,task_id TEXT,task_revision INT,outcome TEXT,status TEXT,created_at REAL,result_json TEXT);''')
        self.run={'id':f['run_id'],'project_id':archive['project_id'],'status':'ACTIVE','sealed':False,'graph_version':f['graph_version'],
                  'authorization_version':f['authorization_version'],'authorization':f['authorization'],'dependencies':f['dependencies'],
                  'control_jobs':[{'id':f['planning_request_id']}],'tasks':[]}
        for item in f['tasks']:
            row=copy.deepcopy(item);row.update(selected=1,lease_id='',repair_count=0,user_revision_count=0,review_again_count=0)
            row['candidate_result']=row.get('accepted_result',[]) or list(row['quality'].get('output_roles',{}).values());self.run['tasks'].append(row)
            self.db.execute('INSERT INTO workflow_task_revisions VALUES (?,?,?,?,?)',(f['run_id'],row['id'],row['revision'],json.dumps(row['spec']),f['graph_version']))
            self.db.execute('INSERT INTO task_results VALUES (?,?,?,?,?,?,?)',(f['run_id'],row['id'],row['revision'],'succeeded','committed',0,
                json.dumps({'quality':row['quality'],'output_roles':row['quality'].get('output_roles',{})})))
        for item in archive['audit']['task_revisions']:
            if item['created_graph_version']<=f['graph_version'] and not self.query('SELECT spec_json FROM workflow_task_revisions WHERE run_id=? AND task_id=? AND revision=?',(f['run_id'],item['task_id'],item['revision'])):
                self.db.execute('INSERT INTO workflow_task_revisions VALUES (?,?,?,?,?)',(f['run_id'],item['task_id'],item['revision'],item['spec_json'],item['created_graph_version']))
        for item in archive['audit']['step_checkpoints']:
            self.db.execute('INSERT INTO execution_step_checkpoints VALUES (?,?,?,?,?)',(f['run_id'],item['step'],item['revision'],archive['project_id'],item['manifest']))
        for item in archive['audit']['plan_selections']:
            if item['graph_version']<=f['graph_version']:self.db.execute('INSERT INTO workflow_plan_tasks VALUES (?,?,?,?)',(f['run_id'],item['graph_version'],item['task_id'],item['task_revision']))
        for item in archive['audit']['authorizations']:
            auth=json.loads(item['authorization_json']);auth['agent_profile_digests']={name:graph.digest(policy(name).snapshot()) for name in auth['allowed_agent_profiles']}
            self.db.execute('INSERT INTO run_authorizations VALUES (?,?,?)',(f['run_id'],item['version'],json.dumps(auth)))
        self.objects={}
        for manifest, in self.db.execute('SELECT manifest FROM execution_step_checkpoints'):
            for record in json.loads(manifest).values():self.objects[record['artifact']]={'sha256':record['sha256'],'size':record['size']}
        self.db.execute('PRAGMA query_only=ON')
    def query(self,sql,args=()):
        if not sql.lstrip().upper().startswith('SELECT'):raise RuntimeError('Evaluation cannot mutate state')
        try:return [dict(x) for x in self.db.execute(sql,args)]
        except sqlite3.Error as e:raise EvaluationEvidenceMissing(type(e).__name__) from None
    def now(self):return time.time()
    def assets(self,project):return self.f['assets']
    def record(self,project,identifier):
        if identifier not in self.objects:raise EvaluationEvidenceMissing('Object metadata missing')
        return self.objects[identifier]


def validate(result,case,store):
    f=case['frozen'];value=graph.ManagerResult.model_validate(result)
    expected={'snapshot_digest':f['snapshot_digest'],'base_graph_version':f['graph_version'],'authorization_version':f['authorization_version'],
              'input_through_sequence':(f.get('input_batch') or {}).get('through_sequence',0)}
    if any(getattr(value,k)!=v for k,v in expected.items()):raise ValueError('Original request identity mismatch')
    if f.get('scope_confirmation_required'):
        if value.kind not in ('REQUEST_AUTHORIZATION','REQUEST_USER_INPUT','NEW_RUN_REQUIRED'):raise ValueError('Unconfirmed successor scope')
    if value.proposal:
        auto,user=scopes(f);graph.validate_proposal(store,store.run,value.proposal.model_dump(),repair_scope=auto,user_revision_scope=user)
    if value.kind=='CONTINUE' and not f.get('input_batch'):raise ValueError('CONTINUE requires frozen input batch')
    if value.kind=='REQUEST_AUTHORIZATION' and value.authorization.goal!=f['goal']:raise ValueError('Authorization cannot change original goal')
    progress=value.kind in (('REQUEST_AUTHORIZATION',) if f.get('scope_confirmation_required') else ('APPLY_PLAN','COMPLETE'))
    return value.kind,progress


def config_list():
    if not os.environ.get('VIDEO_USE_MODEL_BASE_URL'):
        from deploy import dynamic_cluster as c
        os.environ.update(c.environment())
    from webapp.agent_profiles import profiles
    p=profiles()
    return {'gpt':p['default'],'claude':p['claude'],'ds41':dataclasses.replace(p['deepseek'],model='deepseek-v4.1-flash')}


def run_trial(case,model_name,variant,repeat,configs,max_corrections=2):
    if (EVAL/'stop').exists():return {'user_stopped':True}
    directory=EVAL/'results'/variant/model_name/case['id']/str(repeat)
    if (directory/'result.json').exists():return json.loads((directory/'result.json').read_text())
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    config=configs[model_name];adapter=adapter_for(config.protocol);base,key=config.credentials();f=case['frozen'];p=policy('media_planner')
    draft_mode=variant=='draft_strict';compact=variant.startswith('commands') or draft_mode;native=variant!='baseline';strict=variant in ('commands_strict','legacy_strict','commands_strict_generic_feedback','draft_strict')
    draft=None
    if draft_mode:
        from webapp.workflow_plan_draft import EMPTY
        draft=copy.deepcopy(EMPTY);p=dataclasses.replace(p,action_capabilities=('PROPOSE',))
    protocol=ToolProtocol(p,f,{'control_kind':'PLAN'},finalize=not draft_mode,contracts=f.get('provided_operation_contracts',{}),draft=draft)
    if compact:instructions=model_instructions(p,control_kind='PLAN',native_tools=True);context=protocol.context();definitions=protocol.definitions()
    else:
        instructions=(EVAL/'baseline-instructions.txt').read_text();context=copy.deepcopy(f)
        definitions=[{'name':'submit_plan','description':'Submit the frozen ManagerResult. Maps use key/value entries.','schema':schema_for(graph.ManagerResult.model_json_schema())}]
        if native:instructions+='\nFor this evaluation, use the submit_plan native tool with ManagerResult arguments; the native parameter schema supersedes the prose action envelope.'
    instructions+='\nThis is the finalize phase of an isolated plan-submission evaluation. All supplied facts are frozen. Submit a plan or required authorization decision from existing evidence; do not call inspect/contract or execute anything. Multiple legal plans are acceptable.'
    text=graph.encode(context);feedback='';store=FrozenStore(case);calls=[];valid=False;progress=False;kind=None;started=time.time()
    result={'case':case['id'],'split':case['split'],'source_case':case['source_case'],'model':config.model,'variant':variant,'repeat':repeat,
            'started_at':started,'business_actions_executed':0,'validation_scope':'frozen proposal contract; persistence-only counters normalized; no transaction or media execution'}
    correction_count=0;draft_mutations=0
    for turn in range(10 if draft_mode else max_corrections+2):
        prepared_candidate=None
        if (EVAL/'stop').exists():result['user_stopped']=True;break
        attempts=[call for call in calls if not (call.get('http_status') in (400,422) and call.get('fallback')=='non_strict_tools')]
        if (not draft_mode and len(attempts)>=max_corrections+1) or correction_count>max_corrections:break
        if draft_mode:
            protocol=ToolProtocol(p,f,{'control_kind':'PLAN'},contracts=f.get('provided_operation_contracts',{}),draft=draft,
                                  finalize=draft_mutations>=4 or turn>=6)
            definitions=protocol.definitions()
        body=adapter.conversation(instructions,text+feedback)
        if native:body=tool_request(adapter,body,definitions,strict=strict)
        from webapp.workflow_agents import model_output_limit
        body=adapter.prepare(body,config.model,model_output_limit(p,protocol) if compact else 12000,config.reasoning_effort);assert_no_credentials(graph.encode(body),(key,))
        record={'turn':turn,'strict_requested':strict if native else False,'request_sha256':graph.digest(body),'at':time.time()}
        if native:
            record['wire_strict_settings']=[(tool.get('function',tool)).get('strict','omitted') for tool in body['tools']]
        save(directory/f'call-{turn}-intent.json',record)
        try:
            with session() as client:r=client.post(base+adapter.path,json=body,headers=adapter.headers(key),timeout=(10,180),allow_redirects=False)
            record.update(http_status=r.status_code,seconds=time.time()-record['at']);payload=r.json()
            if r.status_code!=200:
                record['error_type']='provider_http';record['error_code']=payload.get('error',{}).get('code');record['dependency_error']=True
                if strict and strict_unsupported(r.status_code,payload):strict=False;record['fallback']='non_strict_tools';calls.append(record);save(directory/f'call-{turn}.json',record);continue
                calls.append(record);save(directory/f'call-{turn}.json',record);break
            record['usage']=payload.get('usage');record['response_status']=payload.get('status') or payload.get('stop_reason')
            response_model=payload.get('model')
            if isinstance(response_model,str):record['response_model']=response_model[:200]
            record['response_strict_settings']=[tool.get('strict','omitted') for tool in payload.get('tools',[]) if tool.get('type')=='function']
            receipt=adapter.receipt();receipt.feed(graph.encode(payload).encode());receipt.finish();record['usage_normalized']=receipt.usage
            usage=record['usage'] or {};record['output_usage_suspect']=usage.get('output_tokens',usage.get('completion_tokens'))==0
            if native:
                name,args=tool_response(adapter,payload);assert_no_credentials(graph.encode(args),(key,));record['syntax_valid']=True
                save(directory/f'reply-{turn}.json',{'tool':name,'arguments':args})
                if compact:
                    decoded=protocol.decode(name,args)
                    if draft_mode and decoded['action']=='draft_batch_commit':
                        from webapp.workflow_plan_draft import apply_value,compile_value
                        command=decoded['command'];commit={key:command[key] for key in ('kind','reason','continuation')}
                        prepared_candidate=apply_value(draft,'draft_batch',{key:value for key,value in command.items() if key not in commit})
                        decoded={'action':'proposal','proposal':compile_value(prepared_candidate,f,commit)}
                    if draft_mode and decoded['action'].startswith('draft_'):
                        from webapp.workflow_plan_draft import apply_value,summary
                        draft=apply_value(draft,decoded['action'],decoded['command'])
                        draft.pop('last_error_signature',None)
                        draft_mutations+=1
                        record.update(schema_valid=True,draft_command=decoded['action'],draft_revision=len(draft['_commands']))
                        text+='\nServer-owned draft command accepted; no business action executed. Current bounded structure:'+graph.encode(summary(draft))
                        feedback=''
                        calls.append(record);save(directory/f'call-{turn}.json',record);continue
                    reply=decoded['proposal']
                else:
                    if name!='submit_plan':raise ValueError('Wrong submission tool')
                    reply=transform(args,graph.ManagerResult.model_json_schema())
            else:
                public=adapter.response_text(payload);assert_no_credentials(public,(key,));save(directory/f'reply-{turn}.json',{'public_text':public})
                raw=decode_reply(public);record['syntax_valid']=True
                if raw.get('action')!='proposal':raise ValueError('Finalize requires proposal')
                reply=raw.get('proposal')
            graph.ManagerResult.model_validate(reply);record['schema_valid']=True
            kind,progress=validate(reply,case,store);valid=True
            if prepared_candidate is not None:draft=prepared_candidate;draft_mutations+=1
            record.update(contract_valid=True,goal_progress=progress,kind=kind)
        except Exception as exc:
            from requests import RequestException
            from pydantic import ValidationError as PydanticError
            from jsonschema import ValidationError as SchemaError
            if isinstance(exc,SchemaError):
                from webapp.strict_schema import validation_feedback
                errors=validation_feedback(exc)
                if strict and getattr(exc,'transport_stage',None)=='wire':
                    strict=False;record['fallback']='strict_constraints_not_enforced';record['strict_constraint_violation']=True
            elif isinstance(exc,PydanticError):errors=[{'path':'.'.join(map(str,e['loc'])),'type':e['type'],'message':e['msg'][:240]} for e in exc.errors(include_input=False,include_url=False)[:12]]
            else:errors=[{'path':getattr(exc,'path','proposal'),'type':getattr(exc,'code',type(exc).__name__),'message':str(exc)[:500]}]
            for e in errors:assert_no_credentials(e['message'],(key,))
            record.update(error_type=type(exc).__name__,errors=errors,seconds=time.time()-record['at'])
            mechanical=bool(record.get('fallback')=='strict_constraints_not_enforced')
            if not mechanical:correction_count+=1
            if draft_mode and not mechanical:draft['last_error_signature']=graph.digest(errors)
            if draft_mode:
                corrections=[name for name in protocol.schemas if name.startswith(('replace_','remove_draft'))]
                if corrections:errors=[{**error,'allowed_actions':corrections} for error in errors]
            feedback='\nLatest submission was rejected before any action. Correct only the reported fields within the same frozen scope. Errors:'+graph.encode(errors)
            if variant=='commands_strict_generic_feedback':feedback='\nThe previous submission was invalid. Correct it using the declared schema within the original scope. Nothing was applied.'
            if isinstance(exc,EvaluationEvidenceMissing):record['unscorable']=True
            if isinstance(exc,RequestException):record['dependency_error']=True
        calls.append(record);save(directory/f'call-{turn}.json',record)
        if valid or record.get('unscorable') or record.get('dependency_error'):break
    store.db.close()
    replies=[call for call in calls if call.get('http_status')==200];first=replies[0] if replies else {}
    result.update(finished_at=time.time(),seconds=time.time()-started,calls=calls,contract_valid=valid,goal_progress=progress,kind=kind,
                  first_syntax_valid=bool(first.get('syntax_valid')),first_schema_valid=bool(first.get('schema_valid')),
                  first_contract_valid=bool(first.get('contract_valid')),unscorable=any(c.get('unscorable') for c in calls),
                  dependency_error=any(c.get('dependency_error') and not c.get('fallback') for c in calls))
    save(directory/'result.json',result)
    print(json.dumps({k:result[k] for k in ('case','model','variant','repeat','contract_valid','goal_progress','seconds','unscorable')}),flush=True)
    return result


def run(args):
    from tests.campaign_model_fixture import validate as validate_fixture
    dataset=json.loads((EVAL/'dataset.json').read_text())
    validate_fixture(dataset,lambda name:graph.digest(policy(name).snapshot()))
    configs=config_list();cases=dataset['cases']
    if args.cases:cases=[c for c in cases if c['id'] in args.cases.split(',')]
    jobs=[(case,model,variant,n) for case in cases for n in range(args.repeat) for variant in args.variants.split(',') for model in args.models.split(',')]
    save(EVAL/'active-evaluation.json',{'started_at':time.time(),'trial_count':len(jobs),'variants':args.variants,'models':{k:v.public() for k,v in configs.items()},'repeat':args.repeat,'jobs':args.jobs,'business_actions_executed':0})
    failures=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures={pool.submit(run_trial,*job,configs):job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:future.result()
            except Exception as exc:
                job=futures[future];failures.append({'case':job[0]['id'],'model':job[1],'variant':job[2],'type':type(exc).__name__});print(json.dumps({'evaluator_error':failures[-1]}),flush=True)
    save(EVAL/'evaluator-errors.json',failures)
    if failures:raise RuntimeError('Evaluation infrastructure errors must be fixed before counting results')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['build','rebind','run']);p.add_argument('--cases');p.add_argument('--models',default='gpt,claude,ds41');p.add_argument('--variants',default='baseline,commands_strict');p.add_argument('--repeat',type=int,default=3);p.add_argument('--jobs',type=int,default=6);a=p.parse_args()
    build() if a.action=='build' else rebind() if a.action=='rebind' else run(a)
