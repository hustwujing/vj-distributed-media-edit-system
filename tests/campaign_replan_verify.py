"""Read-only receipt and accounting checks for the real R17 browser run."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from campaign_interventions import ROOT,configure,save

def verify(directory):
    directory=Path(directory).resolve()
    if directory.parent!=(ROOT/'runs/R17').resolve():raise ValueError('Not an R17 attempt')
    state=json.loads((directory/'state.json').read_text());assert state['case']=='R17'
    cluster,release=configure();assert release['sha256']==state['release']['sha256']
    from webapp.store import Store
    from webapp import workflows,workflow_reuse
    store=Store(cluster.data('node','a'),initialize_schema=False)
    project=state['project'];old=workflows.get(store,state['old_execute']);new=workflows.get(store,state['execute_id'])
    assert old['project']==new['project']==project
    assert old['status']=='cancelled' and new['status']=='succeeded'
    source=next(s for s in old['steps'] if s['id']==state['source_step']);assert source['status']=='succeeded'
    assert source['spec']['parameters']['parameters']['brightness']==1.1
    target=next(s for s in new['steps'] if (s['spec'].get('reuse_from') or {}).get('step')==source['id'])
    requested=target['spec']['reuse_from'];actual=target['bindings']['_reuse']
    assert requested['workflow']==old['id']
    assert all(actual[k]==requested[k] for k in ('workflow','step','revision'))
    receipt=workflow_reuse.source_receipt(store,project,requested)
    hashes=sorted(r['sha256'] for r in receipt['manifest'].values())
    assert sorted(f['sha256'] for f in target['quality']['files'])==hashes
    assert all(f.startswith(f'edit/workflows/{new["id"]}/{target["id"]}/') for f in target['result'])
    budget=store.query("SELECT used FROM execution_budgets WHERE turn_id=? AND category='local_image'",(state['execute_id'],))
    from webapp.operations import for_step
    # All fixed-image operations consume this quota, including export/resize.
    # Count newly executed operations, not only brightness adjustments.
    expected=sum(for_step(s['spec']).executor=='fixed_image' and not s['spec'].get('reuse_from') for s in new['steps'] if s['spec']['kind']=='operation')
    assert (budget[0]['used'] if budget else 0)==expected,'Reuse repeated original operation'
    calls=store.query("SELECT operation,status FROM usage_calls WHERE task_id=? AND category='llm' AND operation LIKE 'control:%'",(state['old_execute'],))
    assert len(calls)>=3 and all(c['status']=='succeeded' for c in calls),calls
    parent=store.query('SELECT parent_task_id FROM usage_tasks WHERE task_id=?',(state['new_plan'],))[0]
    assert parent['parent_task_id']==state['old_execute']
    from PIL import Image
    import numpy as np
    source_record=receipt['manifest'][source['quality']['output_roles']['primary_image']]
    source_path=store.artifacts.materialize(project,source_record['artifact'])
    original=Image.open(source_path).convert('RGBA')
    output=Image.open(directory/'replanned-output.png').convert('RGBA');assert output.size==(32,24)
    errors={method.name:float(np.abs(np.asarray(output).astype(float)-np.asarray(original.resize((32,24),method)).astype(float)).mean())
            for method in (Image.Resampling.LANCZOS,Image.Resampling.BICUBIC,Image.Resampling.BILINEAR)}
    assert min(errors.values())<3,('Output does not match resized reused source',errors)
    proof={'passed':True,'source':actual,'sha256':hashes,'local_image_calls':budget[0]['used'] if budget else 0,'expected_local_image_calls':expected,'real_control_calls':calls,'pixel_mae_by_resampler':errors}
    save(directory/'reuse-proof.json',proof);return proof

def capture(directory):
    directory=Path(directory).resolve()
    if directory.parent!=(ROOT/'runs/R17').resolve():raise ValueError('Not an R17 attempt')
    state=json.loads((directory/'state.json').read_text());project=state.get('project')
    if not project:return {'captured':False}
    cluster,_=configure()
    from webapp.store import Store
    store=Store(cluster.data('node','a'),initialize_schema=False)
    result={
      'readiness':store.query('SELECT * FROM plan_readiness WHERE project=?',(project,)),
      'project_checkpoints':store.query('SELECT * FROM execution_checkpoints WHERE project=?',(project,)),
      'step_checkpoints':store.query('SELECT * FROM execution_step_checkpoints WHERE project=?',(project,)),
      'workflow_definitions':store.query('SELECT id,turn_id,status,definition FROM agent_workflows WHERE project=?',(project,)),
      'usage_calls':store.query('SELECT u.* FROM usage_calls u JOIN agent_turns t ON t.id=u.task_id WHERE t.project=? ORDER BY u.seq',(project,)),
      'budgets':store.query('SELECT b.* FROM execution_budgets b JOIN agent_turns t ON t.id=b.turn_id WHERE t.project=?',(project,)),
      'receipts':store.query('SELECT * FROM workflow_reuse_receipts WHERE project=?',(project,)),
      'events':store.query('SELECT e.* FROM agent_events e JOIN agent_turns t ON t.id=e.turn_id WHERE t.project=? ORDER BY e.seq LIMIT 3000',(project,))}
    save(directory/'database-evidence.json',result);return {'captured':True}

if __name__=='__main__':print(json.dumps(capture(sys.argv[1]) if '--capture' in sys.argv else verify(sys.argv[1])))
