"""Content-based review identity, independent of graph counters and task labels."""
import json
from .workflow_graph import digest,encode


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
    return digest({'scope':frozen['review_scope'],'goal':frozen['goal'],
        'criteria':frozen.get('review_criteria',frozen.get('delivery_targets',[])),
        'candidates':sorted(candidates,key=encode),'verified_evidence':sorted(verified)})
