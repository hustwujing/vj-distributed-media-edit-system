"""Conversation facts and explicit approval; neither grants implicit execution."""
import json
from . import workflow_graph as graph


def context(store,run):
    history=[];identifier=run.get('parent_run_id');seen={run['id']}
    while identifier and identifier not in seen and len(history)<8:
        seen.add(identifier)
        rows=store.query('SELECT id,parent_run_id,goal,status,delivery_json FROM runs WHERE id=? AND project_id=?',
                         (identifier,run['project_id']))
        if not rows:break
        parent=rows[0]
        history.append({'run_id':identifier,'goal':parent['goal'],'state':parent['status'],
            'delivery':[{key:item.get(key) for key in ('name','file','type','size','sha256')}
                        for item in json.loads(parent['delivery_json'])[:20]],
            'steps':store.query('SELECT id,spec_json,status FROM run_tasks WHERE run_id=? AND selected=1 ORDER BY created_graph_version,created_at LIMIT 32',(identifier,)),
            'answers':store.query("SELECT content FROM session_messages WHERE run_id=? AND role='assistant' ORDER BY created_at DESC LIMIT 3",(identifier,))})
        for step in history[-1]['steps']:
            spec=json.loads(step.pop('spec_json'));step.update(name=spec.get('name',''),objective=spec.get('objective',''))
        identifier=parent['parent_run_id']
    questions=[]
    for row in store.query("SELECT id,status,request_json,answer_json FROM workflow_decisions WHERE run_id=? AND kind='input_question' ORDER BY created_at DESC LIMIT 8",(run['id'],)):
        request=json.loads(row.pop('request_json'));answer=json.loads(row.pop('answer_json'))
        questions.append({**row,'question':request.get('question',''),'reason':request.get('reason',''),
                          'answer':{key:answer.get(key) for key in ('action','feedback')}})
    return {'previous_runs':history,'clarifications':list(reversed(questions)),
            'recent_answers':store.query("SELECT content FROM session_messages WHERE run_id=? AND role='assistant' ORDER BY created_at DESC LIMIT 8",(run['id'],)),
            'scope':'Read-only historical facts, not new permissions or authorized input artifacts.'}


def can_respond(run):
    return not run.get('tasks') and run.get('graph_version',0)==0


def respond(store,run,request,result):
    from .run_inputs import finish_batch
    if not can_respond(run) or store.query('SELECT request_key FROM workflow_provider_calls WHERE workflow=? LIMIT 1',(run['id'],)):
        raise ValueError('RESPOND cannot finish a Run with business work; use independent final review')
    now=store.now()
    store.execute('''INSERT INTO session_messages(id,session_id,run_id,role,content,created_at)
        VALUES (?,?,?,'assistant',?,?) ON CONFLICT(id) DO NOTHING''',
        ('answer:'+request['id'],run['session_id'],run['id'],result.reason,now))
    finish_batch(store,run,result.input_through_sequence)
    if run['input_sequence']<=result.input_through_sequence:
        # delivered_at also permits subsequent conversation input. No artifact,
        # accepted Task or media delivery is fabricated by this read-only reply.
        store.execute("UPDATE runs SET status='SUCCEEDED',terminal_reason='conversation_answer',delivered_at=?,sealed=1 WHERE id=?",(now,run['id']))
    graph.event(store,run,'conversation_answered',{'request_id':request['id']})


def needs_confirmation(run,frozen):
    return bool(run['authorization'].get('require_plan_confirmation') and
                (run['graph_version']==0 or frozen.get('input_batch')))


def hold_plan(store,run,request,result,tasks):
    from .workflow_orchestrator import Orchestrator
    frozen=json.loads(request['snapshot_json'])
    if run['input_sequence']>result.input_through_sequence:
        from .run_inputs import defer_batch
        defer_batch(store,run,result.input_through_sequence)
        store.execute("UPDATE planner_runs SET status='rejected',error_code='new_input_barrier' WHERE id=?",(request['id'],))
        store.execute("UPDATE workflow_plan_drafts SET status='EXPIRED',updated_at=? WHERE planning_request_id=?",(store.now(),request['id']))
        store.execute("UPDATE control_jobs SET status='cancelled',updated_at=? WHERE run_id=? AND id=?",(store.now(),run['id'],request['task_id']))
        return
    batch=frozen.get('input_batch') or {};inputs=batch.get('inputs',[])
    previous=run['authorization']['delivery_targets']
    if not previous:
        previous=next(((item.get('context') or {}).get('superseded_plan',{}).get('delivery_targets',[]) for item in reversed(inputs)
                       if (item.get('context') or {}).get('superseded_plan')),[])
    targets=(result.delivery_targets if inputs and result.delivery_targets else run['authorization']['delivery_targets']) or result.delivery_targets
    if not targets or any(not value.strip() or len(value)>500 for value in targets):
        raise ValueError('Plan confirmation requires nonempty delivery_targets; propose concrete outputs for the user')
    changed=bool(inputs and previous and set(previous)!=set(targets))
    from .effective_delivery import change_details
    Orchestrator(store).decision(run,'plan_confirmation',{
        'planning_request_id':request['id'],'proposal_digest':graph.digest(json.loads(request['proposal_json'])),
        'input_sequence':run['input_sequence'],'input_through_sequence':result.input_through_sequence,
        'content_fingerprint':graph.content_fingerprint(store,run['project_id']),
        'reason':result.reason,'question':'请核对方案、关键参数和交付内容。确认后才会执行。',
        'delivery_targets':targets,'previous_delivery_targets':previous,'delivery_scope_changed':changed,
        'delivery_changes':change_details(frozen,targets) if changed else {},
        'steps':[task.model_dump() for task in [*tasks,*result.proposal.revise_tasks]],
        'dependencies':[edge.model_dump() for edge in result.proposal.add_dependencies]})
    store.execute("UPDATE planner_runs SET status='awaiting_confirmation' WHERE id=?",(request['id'],))
    store.execute("UPDATE control_jobs SET status='succeeded',updated_at=? WHERE run_id=? AND id=?",(store.now(),run['id'],request['task_id']))


def invalidate_plan(store,run,decision):
    from .run_inputs import defer_batch
    request=json.loads(decision['request_json'])
    identifier=request['planning_request_id']
    store.execute("UPDATE planner_runs SET status='rejected',error_code='plan_changed' WHERE id=? AND status='awaiting_confirmation'",(identifier,))
    store.execute("UPDATE workflow_plan_drafts SET status='EXPIRED',updated_at=? WHERE planning_request_id=?",(store.now(),identifier))
    defer_batch(store,run,request.get('input_through_sequence',0))
    store.execute("UPDATE workflow_decisions SET status='superseded',updated_at=? WHERE id=?",(store.now(),decision['id']))
