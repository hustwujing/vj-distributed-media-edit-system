"""Server-owned, snapshot-bound planning drafts with no business side effects."""
from copy import deepcopy
import json

from .workflow_graph import digest,encode
from .workflow_model_protocol import CommandError,compile_manager


SCHEMA_VERSION='plan-draft-v1'
LIST_FIELDS=('new_tasks','changes','dependencies','remove_dependencies','repair_replacements')
MAP_FIELDS=('replacements','review_again')
EMPTY={**{key:[] for key in LIST_FIELDS},**{key:{} for key in MAP_FIELDS},'_commands':[]}


def summary(value):
    return {'revision':len(value.get('_commands',[])),
        'tasks':[{'id':item['id'],'type':item['type'],
                  **({'operation':item['operation']} if item.get('operation') else {}),
                  **({'profile':item['profile']} if item.get('profile') else {})} for item in value.get('new_tasks',[])],
        'changes':[item['task_id'] for item in value.get('changes',[])],
        'dependencies':[{'upstream':item['upstream'],'downstream':item['downstream']} for item in value.get('dependencies',[])],
        'remove_dependencies':[{'upstream':item['upstream'],'downstream':item['downstream']} for item in value.get('remove_dependencies',[])],
        'replacement_keys':sorted(value.get('replacements',{})),
        'repair_replacement_tasks':[item['task_id'] for item in value.get('repair_replacements',[])],
        'review_again_tasks':sorted(value.get('review_again',{}))}


def create(store,request_id,run_id,frozen,now):
    store.execute('''INSERT INTO workflow_plan_drafts(
        planning_request_id,run_id,snapshot_digest,base_graph_version,authorization_version,
        input_through_sequence,schema_version,status,draft_json,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,'OPEN',?,?,?)''',(
        request_id,run_id,digest(frozen),frozen['graph_version'],frozen['authorization_version'],
        (frozen.get('input_batch') or {}).get('through_sequence',0),SCHEMA_VERSION,encode(EMPTY),now,now))


def get(store,request_id):
    rows=store.query('SELECT * FROM workflow_plan_drafts WHERE planning_request_id=?',(request_id,))
    if not rows:raise ValueError('Planning draft is unavailable')
    row=rows[0];row['draft']=json.loads(row['draft_json'])
    return row


def _bounded(value):
    limits={'new_tasks':64,'changes':16,'dependencies':512,'remove_dependencies':512,'repair_replacements':16,
            'replacements':16,'review_again':16}
    for field,limit in limits.items():
        if len(value[field])>limit:raise CommandError('draft_limit_exceeded','draft.'+field,'Planning draft exceeds its bounded field limit')
    identifiers=[task.get('id') for task in value['new_tasks']]
    if None in identifiers or len(identifiers)!=len(set(identifiers)):
        raise CommandError('duplicate_task_id','draft.new_tasks','Each draft Task ID must be unique')
    changed=[item.get('task_id') for item in value['changes']]
    if len(changed)!=len(set(changed)):
        raise CommandError('duplicate_change','draft.changes','Change each existing Task at most once')
    edges=[(item.get('upstream'),item.get('downstream')) for item in value['dependencies']]
    if len(edges)!=len(set(edges)):raise CommandError('duplicate_dependency','draft.dependencies','Each dependency must be unique')


def apply_value(current,action,payload,command_digest=None):
    value=deepcopy(current)
    value.setdefault('_commands',[])
    command_digest=command_digest or digest({'action':action,'payload':payload})
    if command_digest in value['_commands']:return value
    if action=='draft_add_tasks':value['new_tasks'].extend(payload['tasks'])
    elif action=='draft_batch':
        value['new_tasks'].extend(payload.get('tasks',[]))
        for field in LIST_FIELDS[1:]:value[field].extend(payload.get(field,[]))
        for field in MAP_FIELDS:
            overlap=set(value[field])&set(payload.get(field,{}))
            if overlap:raise CommandError('duplicate_draft_key','draft.'+field,'A draft mapping key was already declared')
            value[field].update(payload.get(field,{}))
    elif action=='draft_replace_tasks':
        existing={task['id']:index for index,task in enumerate(value['new_tasks'])}
        for task in payload['tasks']:
            if task['id'] not in existing:
                raise CommandError('draft_task_not_found','draft.new_tasks','Only an existing draft Task can be replaced')
            old=value['new_tasks'][existing[task['id']]]
            if old['type']!=task['type'] or old.get('operation')!=task.get('operation'):
                raise CommandError('draft_task_identity_changed','draft.new_tasks','Task type and Operation identity cannot change during local correction')
            value['new_tasks'][existing[task['id']]]=task
    elif action=='draft_remove_tasks':
        identifiers=set(payload['task_ids']);existing={task['id'] for task in value['new_tasks']}
        if not identifiers<=existing:raise CommandError('draft_task_not_found','draft.task_ids','Only existing draft Tasks can be removed')
        value['new_tasks']=[task for task in value['new_tasks'] if task['id'] not in identifiers]
        value['dependencies']=[edge for edge in value['dependencies'] if edge['upstream'] not in identifiers and edge['downstream'] not in identifiers]
        value['remove_dependencies']=[edge for edge in value['remove_dependencies'] if edge['upstream'] not in identifiers and edge['downstream'] not in identifiers]
        value['replacements']={key:item for key,item in value['replacements'].items() if item not in identifiers}
    elif action=='draft_configure':
        for field in LIST_FIELDS[1:]:
            if field in payload:value[field].extend(payload[field])
        for field in MAP_FIELDS:
            if field in payload:
                overlap=set(value[field])&set(payload[field])
                if overlap:raise CommandError('duplicate_draft_key','draft.'+field,'A draft mapping key was already declared')
                value[field].update(payload[field])
    elif action=='draft_replace_configuration':
        for field in (*LIST_FIELDS[1:],*MAP_FIELDS):
            if field in payload:value[field]=deepcopy(payload[field])
    else:raise CommandError('draft_action_unavailable','draft','Unknown planning draft action')
    value['_commands'].append(command_digest);_bounded(value)
    return value


def mutate(store,lease,request_id,action,payload):
    command_digest=digest({'action':action,'payload':payload})
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');lease.validate(db)
        request=db.execute('SELECT * FROM planner_runs WHERE id=?',(request_id,)).fetchone()
        row=db.execute('SELECT * FROM workflow_plan_drafts WHERE planning_request_id=?',(request_id,)).fetchone()
        if not request or not row or request['run_id']!=lease.run_id or request['task_id']!=lease.step_id:
            raise CommandError('draft_scope_changed','draft','Planning draft is outside the active lease')
        if request['status'] not in ('pending','running') or row['status']!='OPEN':
            raise CommandError('draft_not_open','draft','Planning draft is no longer open')
        value=json.loads(row['draft_json'])
        if command_digest in value['_commands']:return value,row['revision']
        value=apply_value(value,action,payload,command_digest)
        revision=row['revision']+1
        db.execute('''UPDATE workflow_plan_drafts SET draft_json=?,revision=?,last_error_signature='',
            repeated_error_count=0,updated_at=? WHERE planning_request_id=? AND revision=?''',
            (encode(value),revision,store.now(),request_id,row['revision']))
        return value,revision


def prepare_batch(store,request_id,frozen,payload,commit):
    row=get(store,request_id)
    if row['status']!='OPEN':raise CommandError('draft_not_open','draft','Planning draft is no longer open')
    candidate=apply_value(row['draft'],'draft_batch',payload)
    proposal=compile_value(candidate,frozen,commit)
    return proposal,row['revision'],digest(row['draft']),candidate,digest(candidate)


def commit_candidate(store,lease,request_id,base_revision,base_digest,candidate,candidate_digest):
    if digest(candidate)!=candidate_digest:raise CommandError('draft_candidate_changed','draft','Prepared draft candidate changed')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');lease.validate(db)
        request=db.execute('SELECT run_id,task_id,status FROM planner_runs WHERE id=?',(request_id,)).fetchone()
        row=db.execute('SELECT status,revision,draft_json FROM workflow_plan_drafts WHERE planning_request_id=?',(request_id,)).fetchone()
        if (not request or not row or request['run_id']!=lease.run_id or request['task_id']!=lease.step_id
                or request['status'] not in ('pending','running') or row['status']!='OPEN'):
            raise CommandError('draft_scope_changed','draft','Planning draft is outside the active lease')
        current=json.loads(row['draft_json'])
        if row['revision']!=base_revision or digest(current)!=base_digest:
            raise CommandError('draft_revision_changed','draft','Planning draft changed before candidate commit')
        revision=base_revision+1
        db.execute('''UPDATE workflow_plan_drafts SET draft_json=?,revision=?,last_error_signature='',
            repeated_error_count=0,updated_at=? WHERE planning_request_id=? AND status='OPEN' AND revision=?''',
            (encode(candidate),revision,store.now(),request_id,base_revision))
        return revision,candidate_digest


def compile(store,request_id,frozen,command):
    row=get(store,request_id)
    if row['status']!='OPEN':raise CommandError('draft_not_open','draft','Planning draft is no longer open')
    if (row['snapshot_digest']!=frozen['snapshot_digest'] or row['base_graph_version']!=frozen['graph_version']
            or row['authorization_version']!=frozen['authorization_version']
            or row['input_through_sequence']!=(frozen.get('input_batch') or {}).get('through_sequence',0)):
        raise CommandError('draft_snapshot_changed','draft','Planning draft binding no longer matches its frozen request')
    return (compile_value(row['draft'],frozen,command),
            row['revision'],digest(row['draft']))


def compile_value(value,frozen,command):
    draft={key:deepcopy(value[key]) for key in (*LIST_FIELDS,*MAP_FIELDS)}
    draft['continuation']=deepcopy(command['continuation'])
    return compile_manager({'kind':command['kind'],'reason':command['reason'],'plan':draft},frozen)


def record_error(store,request_id,errors):
    signature=digest(errors)
    row=get(store,request_id);repeated=row['repeated_error_count']+1 if row['last_error_signature']==signature else 1
    store.execute('''UPDATE workflow_plan_drafts SET last_error_signature=?,repeated_error_count=?,updated_at=?
        WHERE planning_request_id=? AND status='OPEN' ''',(signature,repeated,store.now(),request_id))
    return signature,repeated


def committed(store,request_id):
    store.execute("UPDATE workflow_plan_drafts SET status='COMMITTED',updated_at=? WHERE planning_request_id=? AND status='OPEN'",
                  (store.now(),request_id))


def expire(store,request_id,status='EXPIRED'):
    if status not in ('EXPIRED','REJECTED'):raise ValueError('Invalid draft terminal state')
    store.execute("UPDATE workflow_plan_drafts SET status=?,updated_at=? WHERE planning_request_id=? AND status='OPEN'",
                  (status,store.now(),request_id))
