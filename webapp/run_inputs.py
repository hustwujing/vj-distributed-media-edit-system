"""Durable Run inbox and transactional dispatch barrier.

Receiving input never cancels an attempt or changes its frozen contract. Claims,
input acceptance and final publication all use the Store control transaction.
"""
import json
import uuid
from pydantic import Field,model_validator
from . import workflow_graph as graph


class InputAttachmentChanged(ValueError):
    def __init__(self,identifiers):
        self.input_ids=identifiers
        super().__init__('Input attachments changed or disappeared before the safe point')


class Input(graph.Contract):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    content:str=Field(min_length=1,max_length=8000)
    attachments:list[str]=Field(default_factory=list,max_length=20)
    targets:list[str]=Field(default_factory=list,max_length=16)
    source:str=Field(default='user',pattern=r'^(user|quality_feedback)$')

    @model_validator(mode='after')
    def nonempty(self):
        if not self.content.strip():raise ValueError('Input requires nonempty content')
        if len(set(self.attachments))!=len(self.attachments):raise ValueError('Duplicate attachments')
        if len(set(self.targets))!=len(self.targets):raise ValueError('Duplicate target Tasks')
        return self


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS run_inputs(
        id TEXT PRIMARY KEY,run_id TEXT NOT NULL,submitted_run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,idempotency_key TEXT NOT NULL,identity_digest TEXT NOT NULL,
        actor TEXT NOT NULL,content TEXT NOT NULL,attachments_json TEXT NOT NULL,source TEXT NOT NULL,
        context_json TEXT NOT NULL,base_graph_version INTEGER NOT NULL,base_authorization_version INTEGER NOT NULL,
        status TEXT NOT NULL,accepted_at REAL NOT NULL,applied_at REAL,applied_graph_version INTEGER,successor_run_id TEXT,
        error_code TEXT NOT NULL DEFAULT '',UNIQUE(run_id,sequence),UNIQUE(submitted_run_id,idempotency_key));
      CREATE TABLE IF NOT EXISTS run_input_drain(
        run_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,
        barrier_sequence INTEGER NOT NULL,PRIMARY KEY(run_id,task_id,revision));
    ''')


def public(row):
    row=dict(row)
    row['attachments']=json.loads(row.pop('attachments_json'))
    row['context']=json.loads(row.pop('context_json'))
    row.pop('identity_digest',None)
    return row


def accept(store,identifier,actor,raw,*,context=None):
    request=Input.model_validate(raw)
    context=context or {}
    identity=graph.digest({'request':request.model_dump(),'actor':actor,'context':context})
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT * FROM run_inputs WHERE submitted_run_id=? AND idempotency_key=?',(identifier,request.request_id)).fetchone()
        if previous:
            if previous['identity_digest']!=identity:raise ValueError('Input request identity changed')
            return public(previous)
        run=graph.get(store,identifier)
        if not run:raise ValueError('Unknown Run')
        if run['status'] in ('CANCELLING','FAILED','CANCELLED') or run['status']=='SUCCEEDED' and run['delivered_at'] is None:
            raise ValueError('Run cannot accept business input')
        predecessor_targets=[]
        if request.targets:
            active={task['id'] for task in graph.active_tasks(run)}
            if not set(request.targets)<=active:raise ValueError('Input target is outside the current Run plan')
        owned={asset['id']:asset for asset in store.assets(run['project_id'])}
        if not set(request.attachments)<=set(owned):raise ValueError('Attachment outside project')
        if run['delivered_at'] is not None:
            predecessor_targets=[{'task_id':task['id'],'revision':task['revision'],'objective':task['spec']['objective'],'inputs':task['spec']['inputs']} for task in graph.active_tasks(run) if task['id'] in request.targets]
            # A successor has a fresh authority decision; input alone never grants
            # use of new assets, providers or an altered delivery scope.
            authorization={**run['authorization'],'goal':request.content,
                'content_fingerprint':graph.content_fingerprint(store,run['project_id'])}
            authorization['allowed_assets']=[identifier for identifier in authorization['allowed_assets'] if identifier in owned]
            authorization['allowed_asset_digests']={key:value for key,value in authorization['allowed_asset_digests'].items() if key in owned}
            successors=store.query("SELECT id FROM runs WHERE parent_run_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",(run['id'],))
            if successors:successor=graph.get(store,successors[0]['id'])
            else:
                successor=graph.create(store,run['project_id'],None,authorization,parent_run_id=run['id'],scope_confirmed=False,record_message=False)
            run=graph.get(store,successor['id'])
        pending=db.execute("SELECT content FROM run_inputs WHERE run_id=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],)).fetchall()
        if len(pending)>=100 or sum(len(item['content'].encode()) for item in pending)+len(request.content.encode())>64000:raise ValueError('Run input queue is full')
        if 'allowed_revision_tasks' not in context:
            affected=set()
            targets=request.targets if run['id']==identifier else []
            for target in targets:affected.update(revision_closure(run,target))
            context={**context,'targets':targets,'predecessor_targets':predecessor_targets,'allowed_revision_tasks':sorted(affected) if targets else [task['id'] for task in graph.active_tasks(run)],
                'base_task_revisions':{task['id']:task['revision'] for task in graph.active_tasks(run)}}
        if run['terminal_reason']=='user_rejected' and request.source=='user':
            db.execute("UPDATE runs SET status='ACTIVE',terminal_reason='input_draining' WHERE id=?",(run['id'],))
        sequence=run['input_sequence']+1;input_id=uuid.uuid4().hex;now=store.now()
        attachments=[{'id':name,'authorized':name in run['authorization']['allowed_assets'] and run['authorization']['allowed_asset_digests'].get(name)==graph.asset_digest(owned[name]),
                      'name':owned[name]['name'],'metadata':{key:owned[name]['metadata'][key] for key in ('kind','width','height','duration','size') if key in owned[name]['metadata']},
                      'metadata_digest':graph.digest(owned[name])} for name in request.attachments]
        db.execute('''INSERT INTO run_inputs(id,run_id,submitted_run_id,sequence,idempotency_key,identity_digest,
            actor,content,attachments_json,source,context_json,base_graph_version,base_authorization_version,status,accepted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACCEPTED',?)''',
            (input_id,run['id'],identifier,sequence,request.request_id,identity,actor,request.content,
             graph.encode(attachments),request.source,graph.encode(context),run['graph_version'],run['authorization_version'],now))
        if run['input_barrier_sequence'] is None:
            db.execute('''INSERT INTO run_input_drain(run_id,task_id,revision,barrier_sequence)
                SELECT run_id,id,revision,? FROM run_tasks WHERE run_id=? AND
                (attempts>0 OR lease_id<>'' OR external_id<>'')''',(sequence,run['id']))
        db.execute('''UPDATE runs SET input_sequence=?,input_barrier_was_sealed=CASE WHEN input_barrier_sequence IS NULL THEN sealed ELSE input_barrier_was_sealed END,input_barrier_sequence=COALESCE(input_barrier_sequence,?),
            next_reconcile_at=?,updated_at=? WHERE id=?''',(sequence,sequence,now,now,run['id']))
        db.execute('INSERT INTO session_messages(id,session_id,run_id,role,content,created_at) VALUES (?,?,?,?,?,?)',
            (input_id,run['session_id'],run['id'],'user',request.content,now))
        graph.event(store,run,'input_accepted',{'input_id':input_id,'input_sequence':sequence,'source':request.source})
        return public(db.execute('SELECT * FROM run_inputs WHERE id=?',(input_id,)).fetchone())


def can_dispatch(run,item):
    if run['input_barrier_sequence'] is None or item.get('control_kind'):return True
    return any(entry['task_id']==item['id'] and entry['revision']==item['revision'] for entry in run['drain_set'])


def safe_point_reason(store,run):
    if store.query('SELECT attempt FROM execution_leases WHERE run_id=? AND active=1 AND expires>?',(run['id'],store.now())):return 'draining_attempts'
    if store.query("SELECT attempt FROM execution_cleanup WHERE task_id=? AND state<>'complete'",(run['id'],)):return 'draining_cleanup'
    if store.query("SELECT id FROM task_results WHERE run_id=? AND status='staged'",(run['id'],)):return 'draining_results'
    if store.query("SELECT id FROM workflow_decisions WHERE run_id=? AND status='pending'",(run['id'],)):return 'input_waiting_user_decision'
    calls=store.query("SELECT state FROM workflow_provider_calls WHERE workflow=? AND state NOT IN ('succeeded','failed','cancelled','rejected')",(run['id'],))
    if calls:return 'input_waiting_external_unknown' if any(row['state'] in ('submitting','submission_unknown') for row in calls) else 'draining_provider'
    if any(job['status'] not in ('succeeded','failed','cancelled','paused') for job in run['control_jobs']):return 'draining_control_jobs'
    if any(task['status'] in ('ready','running','waiting','retry_wait','reviewing') and can_dispatch(run,task) for task in run['tasks']):return 'draining_tasks'
    return ''


def freeze_batch(store,run):
    records=store.query("SELECT * FROM run_inputs WHERE run_id=? AND status='ACCEPTED' ORDER BY sequence",(run['id'],))
    if not records:return None
    assets={asset['id']:asset for asset in store.assets(run['project_id'])}
    invalid=[row['id'] for row in records if any(item['id'] not in assets or graph.digest(assets[item['id']])!=item['metadata_digest'] for item in json.loads(row['attachments_json']))]
    if invalid:raise InputAttachmentChanged(invalid)
    cutoff=records[-1]['sequence']
    store.execute("UPDATE run_inputs SET status='PLANNING' WHERE run_id=? AND sequence<=? AND status='ACCEPTED'",(run['id'],cutoff))
    inputs=[public(row) for row in records]
    for item in inputs:
        for attachment in item['attachments']:
            attachment['accepted_authorized']=attachment['authorized']
            attachment['authorized']=run['authorization']['allowed_asset_digests'].get(attachment['id'])==graph.asset_digest(assets[attachment['id']])
    return {'through_sequence':cutoff,'inputs':inputs}


def finish_batch(store,run,cutoff,*,status='APPLIED'):
    store.execute("UPDATE run_inputs SET status=?,applied_at=?,applied_graph_version=? WHERE run_id=? AND sequence<=? AND status='PLANNING'",
        (status,store.now(),run['graph_version'],run['id'],cutoff))
    remaining=store.query("SELECT MIN(sequence) AS first FROM run_inputs WHERE run_id=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],))[0]['first']
    store.execute('UPDATE runs SET input_barrier_sequence=?,input_barrier_was_sealed=?,next_reconcile_at=? WHERE id=?',(remaining,int(graph.get(store,run['id'])['sealed']),store.now(),run['id']))
    if remaining is None:store.execute('DELETE FROM run_input_drain WHERE run_id=?',(run['id'],))
    graph.event(store,run,'input_batch_applied',{'through_sequence':cutoff,'next_barrier_sequence':remaining})


def defer_batch(store,run,cutoff):
    store.execute("UPDATE run_inputs SET status='ACCEPTED' WHERE run_id=? AND sequence<=? AND status='PLANNING'",(run['id'],cutoff))


def reject_batch(store,run,cutoff,error_code='authorization_rejected'):
    rows=store.query("SELECT id FROM run_inputs WHERE run_id=? AND sequence<=? AND status IN ('ACCEPTED','PLANNING')",
                     (run['id'],cutoff))
    supersede_inputs(store,run,[row['id'] for row in rows],error_code,through_sequence=cutoff)


def supersede_inputs(store,run,input_ids,error_code,*,through_sequence=None):
    """Retire selected pending inputs and restore or advance the Run barrier."""
    now=store.now()
    if input_ids:
        placeholders=','.join('?' for _ in input_ids)
        store.execute(f"""UPDATE run_inputs SET status='SUPERSEDED',applied_at=?,error_code=?
            WHERE run_id=? AND id IN ({placeholders}) AND status IN ('ACCEPTED','PLANNING')""",
            (now,error_code,run['id'],*input_ids))
    remaining=store.query("SELECT MIN(sequence) AS first FROM run_inputs WHERE run_id=? AND status IN ('ACCEPTED','PLANNING')",(run['id'],))[0]['first']
    sealed=run['input_barrier_was_sealed'] if remaining is None else run['sealed']
    store.execute('UPDATE runs SET input_barrier_sequence=?,sealed=?,next_reconcile_at=? WHERE id=?',
                  (remaining,int(sealed),now,run['id']))
    if remaining is None:store.execute('DELETE FROM run_input_drain WHERE run_id=?',(run['id'],))
    payload={'input_ids':input_ids,'next_barrier_sequence':remaining,'error_code':error_code}
    if through_sequence is not None:payload['through_sequence']=through_sequence
    graph.event(store,run,'input_batch_rejected',payload)


def candidate_digest(store,run,task):
    rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
        (run['id'],task['id'],task['revision']))
    return graph.digest({'task_id':task['id'],'revision':task['revision'],'candidate':task['candidate_result'],
        'quality':task['quality'],'manifest':json.loads(rows[0]['manifest']) if rows else {}})


def revision_closure(run,subject,upstreams=()):
    ids={task['id'] for task in run['tasks']}
    if subject not in ids or not set(upstreams)<=ids:raise ValueError('Unknown revision subject')
    ancestors={subject}
    while True:
        expanded=ancestors|{edge['upstream_task_id'] for edge in run['dependencies'] if edge['downstream_task_id'] in ancestors}
        if expanded==ancestors:break
        ancestors=expanded
    if not set(upstreams)<=ancestors:raise ValueError('Requested upstream is not an ancestor')
    affected={subject,*upstreams}
    while True:
        expanded=affected|{edge['downstream_task_id'] for edge in run['dependencies'] if edge['upstream_task_id'] in affected}
        if expanded==affected:return sorted(affected)
        affected=expanded
