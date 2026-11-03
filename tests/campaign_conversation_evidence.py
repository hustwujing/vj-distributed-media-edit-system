"""Read-only operator evidence for the deployed conversation acceptance run."""
import json
from pathlib import Path
from campaign_probe import store
from webapp.diagnostics import diagnose
from webapp.task_usage import snapshot
s=store();root=Path('.cluster/campaigns/20261101-conversation');turns=[]
for file in sorted(root.glob('*-turn.json')):
    row=json.loads(file.read_text());task=row['id'];project=row['project']
    attempts=s.query('SELECT id,worker,status,runtime,uid,checkpoint,started,finished FROM execution_attempts WHERE run_id=? ORDER BY started',(task,))
    metrics=s.query('SELECT attempt,phase,calls,seconds,failures FROM execution_phase_metrics WHERE task_id=?',(task,))
    checkpoints=s.query('SELECT attempt,manifest FROM execution_checkpoint_versions WHERE attempt IN (SELECT id FROM execution_attempts WHERE run_id=?)',(task,))
    result={'case':file.stem.removesuffix('-turn'),'task':task,'project':project,'status':row['status'],'mode':row['mode'],'routing':row['routing'],'attempts':attempts,'metrics':metrics,'checkpoints':[]}
    for cp in checkpoints:
        manifest=json.loads(cp['manifest']);result['checkpoints'].append({'attempt':cp['attempt'],'workspace_files':list(manifest.get('workspace',{})),'state':manifest.get('state',{})})
    if row['routing']['stage']=='light':
        assert not checkpoints,(file,'unexpected checkpoint')
        assert all(not a['runtime'] and a['uid'] is None for a in attempts),(file,'unexpected sandbox')
        assert not any(m['phase']=='output_upload' for m in metrics),(file,'unexpected upload')
    for cp in result['checkpoints']:
        assert set(cp['state']) <= {'session.tar.gz'},(file,'unexpected state format')
        assert all('skills' not in p for p in cp['workspace_files']),(file,'skills checkpointed')
    usage=snapshot(s,task,limit=200)
    (root/(result['case']+'-usage.json')).write_text(json.dumps(usage,ensure_ascii=False,indent=2)+'\n')
    diagnostics=diagnose(s,task,0,200)
    (root/(result['case']+'-diagnostics.json')).write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2)+'\n')
    result['diagnostics']={'complete':diagnostics['complete'],'gaps':diagnostics['gaps'],'logs':len(diagnostics['logs']),'has_more':diagnostics['has_more'],'latest_errors':diagnostics['facts']['latest_errors']}
    turns.append(result)
result={'passed':True,'release':json.loads(Path('.cluster/release.json').read_text()),'turns':turns,'database_version':s.query('SELECT version() AS version')[0]['version']}
(root/'database-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':True,'turns':len(turns),'light_no_sandbox':sum(t['routing']['stage']=='light' for t in turns),'checkpoint_versions':sum(len(t['checkpoints']) for t in turns)}))
