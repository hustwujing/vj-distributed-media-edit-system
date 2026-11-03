"""Owner-selected task context for intent classification, never authority to act."""
import hashlib
import json


def build(store,task):
    row=store.query('SELECT id,project,mode,plan_id,prompt,text FROM agent_turns WHERE id=?',(task,))[0]
    plan=store.query('SELECT id,prompt,text FROM agent_turns WHERE id=? AND project=?',(row['plan_id'],row['project'])) if row['plan_id'] else []
    from . import workflows
    from .execution_intent import get as execution_intent
    w=workflows.get(store,task)
    requests=(w or {}).get('user_requests',[])
    steps=(w or {}).get('steps',[])
    context={'task_id':task,'mode':row['mode'],'current_request':row['prompt'][:2000],'latest_public_reply':row['text'][-1800:],
             'approved_plan':{'task_id':plan[0]['id'],'request':plan[0]['prompt'][:2000],'proposal':plan[0]['text'][:6000],'intent':execution_intent(store,plan[0]['id'])} if plan else None,
             'workflow':{'id':w['id'],'name':w['name'],'status':w['status']} if w else None,
             'steps':[{'id':s['id'],'name':s['spec']['name'][:120],'goal':s['spec'].get('goal','')[:200],'revision':s['revision'],'status':s['status']} for s in steps[:24]],
             'pending_questions':[{'id':r['id'],'step':r['step'],'revision':r['revision'],'kind':r['kind'],'question':r['question'][:600],
                                   'options':[v[:160] for v in r.get('options',[])[:6]]} for r in requests[:6]],
             'coverage':{'steps_truncated':len(steps)>24,'questions_truncated':len(requests)>6,'request_truncated':len(row['prompt'])>2000,
                         'reply_truncated':len(row['text'])>1800,'plan_truncated':bool(plan and (len(plan[0]['prompt'])>2000 or len(plan[0]['text'])>6000))}}
    # Progress alone may advance during inference. Versioned plans and pending
    # decisions may not: reject stale interpretation rather than redirect it.
    binding={'task':task,'plan':plan,'intent':context['approved_plan'].get('intent') if plan else None,'workflow_id':w['id'] if w else None,
             'revisions':[(s['id'],s['revision']) for s in steps],
             'requests':[{k:r.get(k) for k in ('id','step','revision','kind','question','options')} for r in requests]}
    context['binding_digest']=hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return context
