"""Archive retained, sanitized telemetry and durable audit history for each real attempt."""
import json
import os
from pathlib import Path
import re
import time


def _archive(directory,db,observation):
    directory=Path(directory).resolve();repo=Path(os.environ.get('DYNAMIC_CAMPAIGN_REPO',Path(__file__).resolve().parents[1]));root=Path(os.environ.get('DYNAMIC_CASE_DEPLOY_ROOT',repo/'.cluster'))/'campaigns/dynamic-plan/runs'
    if not directory.is_relative_to(root.resolve()):raise ValueError('Not a campaign attempt')
    state=json.loads((directory/'state.json').read_text());ids={state.get('plan_id'),state.get('execute_id'),*state.get('task_ids',[])}
    ids.update(p.stem for p in (directory/'plan-history').glob('*.json'))
    folder=directory/'trace-archive';folder.mkdir(mode=0o700,exist_ok=True)
    index=folder/'index.json';result=json.loads(index.read_text()) if index.exists() else {'tasks':{},'scope':'All retained sanitized telemetry at collection time plus durable database audits; pre-retention loss cannot be reconstructed.'}
    for task in sorted(x for x in ids if x):
        if not re.fullmatch('[a-f0-9]{32}',task):raise ValueError('Invalid task identity')
        item=result['tasks'].setdefault(task,{'log_cursor':0,'audit_cursor':0,'logs':0,'audit_events':0})
        try:
            for _ in range(200):
                page=observation('/logs',{'task_id':task,'after':item['log_cursor'],'limit':200})
                events=page['events']
                with (folder/(task+'-logs.jsonl')).open('a') as out:
                    for event in events:out.write(json.dumps(event,ensure_ascii=False)+'\n')
                item['logs']+=len(events);item['log_cursor']=page['next_after']
                if not page['has_more']:break
            else:raise RuntimeError('Telemetry archive page budget exceeded')
            item.pop('error',None)
        except Exception as exc:item['error']=type(exc).__name__
        for _ in range(200):
            events=db.query('SELECT seq,entity,entity_id,old_state,new_state,created FROM observation_audit WHERE task_id=? AND seq>? ORDER BY seq LIMIT 500',(task,item['audit_cursor']))
            with (folder/(task+'-audit.jsonl')).open('a') as out:
                for event in events:out.write(json.dumps(event,ensure_ascii=False)+'\n')
            item['audit_events']+=len(events)
            if events:item['audit_cursor']=events[-1]['seq']
            if len(events)<500:break
        item['collected_at']=time.time()
    result['updated_at']=time.time();index.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    for p in folder.iterdir():p.chmod(0o600)
    return result

def archive(directory,db,observation):
    import fcntl
    directory=Path(directory).resolve();repo=Path(os.environ.get('DYNAMIC_CAMPAIGN_REPO',Path(__file__).resolve().parents[1]));root=Path(os.environ.get('DYNAMIC_CASE_DEPLOY_ROOT',repo/'.cluster'))/'campaigns/dynamic-plan/runs'
    if not directory.is_relative_to(root.resolve()):raise ValueError('Not a campaign attempt')
    with (directory/'trace-archive.lock').open('a') as lock:
        os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        return _archive(directory,db,observation)

if __name__=='__main__':
    from campaign_probe import store,ROOT
    from webapp.diagnostics import observation
    db=store();count=0;logs=0
    for state in sorted((ROOT/'runs').glob('*/attempt-*/state.json')):
        result=archive(state.parent,db,observation);count+=1;logs+=sum(x['logs'] for x in result['tasks'].values())
    print(json.dumps({'attempts':count,'retained_logs_archived':logs}))
