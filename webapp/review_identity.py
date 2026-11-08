"""Content-based review identity, independent of graph counters and task labels."""
import json
from .workflow_graph import digest,encode


def applied_user_changes(store,run):
    changes=store.query("SELECT sequence,content,source,context_json FROM run_inputs WHERE run_id=? AND status='APPLIED' ORDER BY sequence DESC LIMIT 101",(run['id'],))
    for change in changes:change['context']=json.loads(change.pop('context_json'))
    return list(reversed(changes[:100])),len(changes)>100


def final_identity(store,run):
    from .workflow_graph import work_items
    changes,truncated=applied_user_changes(store,run)
    tasks=[task for task in work_items(run) if not task.get('control_kind')]
    return identity(store,run,{'review_scope':'final','goal':run['goal'],
        'delivery_targets':run['authorization']['delivery_targets'],
        'review_criteria':final_criteria(run,tasks),'tasks':tasks,
        'user_changes':changes,'user_changes_truncated':truncated})


def final_criteria(run,tasks):
    criteria=list(dict.fromkeys(run['authorization']['delivery_targets']))
    if not criteria:
        criteria=list(dict.fromkeys(check.get('criterion','') for task in tasks
            if task['spec'].get('delivery_stage') for check in task['spec'].get('acceptance',[])
            if check.get('criterion')))
    # A planner's deliverable summary cannot replace the user's full request.
    # Keep bounded clauses so every part can be cited by the review protocol.
    import textwrap
    goal=str(run.get('goal') or '').strip()
    for part in textwrap.wrap(goal,width=1200,break_long_words=True,break_on_hyphens=False):
        criterion='完整用户目标（已应用的 user_changes 对内容/参数的修订优先）：'+part
        if part not in criteria and criterion not in criteria:criteria.append(criterion)
    return criteria or [goal]


def identity(store,run,frozen):
    if frozen['review_scope']=='diagnose':return digest(frozen)
    candidates=[];verified=set()
    for task in frozen['tasks']+frozen.get('upstream',[])+frozen.get('evidence_tasks',[]):
        if task['spec'].get('purpose')=='evidence':
            proof=task['quality'].get('verified_evidence')
            if proof:verified.add(digest({key:proof.get(key) for key in ('source_sha256','reference_sha256','parameters','kind','preview_sha256','implementation_id')}))
            continue
        rows=store.query('SELECT manifest FROM execution_step_checkpoints WHERE workflow=? AND step=? AND revision=?',
            (run['id'],task['id'],task['revision']))
        manifest=json.loads(rows[0]['manifest']) if rows else {}
        files=sorted({manifest[name]['sha256'] if name in manifest else 'missing-evidence'
            for name in task['candidate_result']+task['accepted_result']})
        from .workflow_graph import authority_for
        scope=authority_for(store,run,task)
        inputs={role:task.get('bindings',{}).get(role,{}).get('sha256') or
                scope['allowed_asset_digests'].get(ref.get('asset')) or
                (ref.get('repair_candidate') or {}).get('sha256')
                for role,ref in task['spec'].get('inputs',{}).items()}
        candidates.append({'content':files,'input_content':inputs,
            'objective':task['spec']['objective'],'acceptance':task['spec']['acceptance'],
            'semantics':task['spec'].get('semantics'),'delivery_stage':task['spec'].get('delivery_stage')})
    context={'scope':frozen['review_scope'],'goal':frozen['goal'],
        'criteria':frozen.get('review_criteria',frozen.get('delivery_targets',[])),
        'candidates':sorted(candidates,key=encode),'verified_evidence':sorted(verified)}
    if frozen['review_scope']=='final':
        # Applied user requirements are final-review criteria, unlike bare graph
        # counters or task labels. Never reuse a verdict that predates them.
        context['user_changes']=[{'content':item.get('content',''),'source':item.get('source',''),
            'targets':(item.get('context') or {}).get('targets',[])} for item in frozen.get('user_changes',[])]
        context['user_changes_truncated']=bool(frozen.get('user_changes_truncated'))
    return digest(context)
