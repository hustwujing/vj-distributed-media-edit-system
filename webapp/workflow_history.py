"""Bounded authoritative history for planning and final review; never authority."""
import json
from . import workflow_graph as graph


def plan_confirmation_evidence(store,run):
    """Process receipts prove presentation/approval, never content acceptance."""
    rows=store.query("SELECT id,graph_version,authorization_version,snapshot_digest,request_json,answer_json,updated_at FROM workflow_decisions WHERE run_id=? AND kind='plan_confirmation' AND status='applied' ORDER BY updated_at DESC LIMIT 101",(run['id'],))
    approved=[]
    for row in rows[:100]:
        request=json.loads(row['request_json']);answer=json.loads(row['answer_json'])
        if answer.get('action')!='approve':continue
        approved.append({'decision_id':row['id'],'planning_request_id':request.get('planning_request_id'),
            'graph_version':row['graph_version'],'authorization_version':row['authorization_version'],
            'snapshot_digest':row['snapshot_digest'],'approved_at':row['updated_at'],
            'delivery_targets':request.get('delivery_targets',[]),
            'task_ids':[step['id'] for step in request.get('steps',[]) if step.get('id')]})
    return {'source':'server_decision_ledger','approved_plans':list(reversed(approved)),
            'history_truncated':len(rows)>100,
            'scope':'Plan presentation and explicit approval only; not a content-review or technical-check waiver.'}


def user_reviews(store,run):
    from .run_inputs import candidate_digest
    selected={task['id']:task for task in graph.active_tasks(run)};result=[]
    for row in store.query("SELECT id,request_json,answer_json,updated_at FROM workflow_decisions WHERE run_id=? AND kind='quality' AND status='applied' ORDER BY updated_at DESC LIMIT 100",(run['id'],)):
        request=json.loads(row['request_json']);answer=json.loads(row['answer_json']);task=selected.get(request.get('task_id'))
        if (answer.get('action')=='approve' and task and task['status']=='succeeded'
            and request['revision']==task['revision'] and request['candidate_digest']==candidate_digest(store,run,task)):
            result.append({'decision_id':row['id'],'task_id':task['id'],'revision':task['revision'],
                'candidate_digest':request['candidate_digest'],'action':'approve','approved_at':row['updated_at']})
    return result


def snapshot(store,run):
    rows=store.query('SELECT task_id,revision,spec_json,created_graph_version,supersedes_revision,repair_root FROM workflow_task_revisions WHERE run_id=? ORDER BY created_graph_version DESC,task_id,revision DESC LIMIT 257',(run['id'],))
    revisions=[];remaining=192000;truncated=len(rows)>256
    for row in rows[:256]:
        spec=json.loads(row.pop('spec_json'));size=len(graph.encode(spec).encode())
        if size>remaining:truncated=True;break
        remaining-=size;revisions.append({**row,'spec':spec,'spec_sha256':graph.digest(spec)})
    dependencies=store.query('SELECT upstream_task_id,downstream_task_id,created_graph_version,retired_graph_version FROM workflow_dependencies WHERE run_id=? ORDER BY created_graph_version DESC,upstream_task_id,downstream_task_id LIMIT 513',(run['id'],))
    selections=store.query('SELECT graph_version,task_id,task_revision FROM workflow_plan_tasks WHERE run_id=? ORDER BY graph_version DESC,task_id LIMIT 513',(run['id'],))
    replacements=store.query('SELECT old_task_id,new_task_id,graph_version,source FROM run_task_replacements WHERE run_id=? ORDER BY graph_version DESC LIMIT 257',(run['id'],))
    return {'task_revisions':revisions,'dependencies':dependencies[:512],'plan_selections':selections[:512],
        'replacements':replacements[:256],
        'retired_tasks':[{key:task[key] for key in ('id','type','revision','status','error_code','error_detail')} for task in run['tasks'] if not task['selected']],
        'truncated':truncated or len(dependencies)>512 or len(selections)>512 or len(replacements)>256,
        'scope':'Server-owned audit facts for this Run. Retired tasks are history, not current delivery requirements. Only current selected accepted outputs can be delivered.'}
