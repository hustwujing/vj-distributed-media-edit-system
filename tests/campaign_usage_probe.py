"""Read-only independent SQL reconciliation of real usage acceptance tasks."""
import json,os,sys,time
from pathlib import Path
REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
from deploy import cluster
release=json.loads((cluster.ROOT/'release.json').read_text());sys.path.insert(0,release['source']);os.environ.update(cluster.environment())
from webapp.store import Store
root=REPO/'.cluster/campaigns/20261101-task-usage';store=Store(cluster.data('node','a'),initialize_schema=False)
results=[]
for path in root.glob('*-usage.json'):
    public=json.loads(path.read_text());task=public['task_id'];members=[r['task_id'] for r in public['members']];marks=','.join('?' for _ in members)
    # A paused task can later gain a new revision. When the saved API detail
    # page contains every call, reconcile its recorded sequence boundary.
    total=sum(v['calls'] for v in public['counts'].values())
    watermark=max((r['seq'] for r in public['calls']),default=0) if len(public['calls'])==total else None
    extra=' AND seq<=?' if watermark is not None else ''
    rows=store.query(f'SELECT * FROM usage_calls WHERE task_id IN ({marks})'+extra+' ORDER BY seq',(*members,watermark) if watermark is not None else members)
    llm=[r for r in rows if r['category']=='llm'];known=[r['total_tokens'] for r in llm if r['total_tokens'] is not None]
    total=sum(known) if known else None
    checks={'llm_count':len(llm)==public['counts']['llm']['calls'],'tokens':total==public['tokens']['total_tokens']['known'],
            'call_ids_unique':len({r['call_id'] for r in rows})==len(rows),
            'tools':sum(r['category']=='tool' for r in rows)==public['counts']['tool']['calls'],
            'workflow':sum(r['category']=='workflow' for r in rows)==public['counts']['workflow']['calls']}
    attempts=store.query(f'SELECT a.id,a.run_id,a.kind,a.status,w.node,w.release FROM execution_attempts a JOIN execution_workers w ON w.id=a.worker WHERE a.run_id IN ({marks})',members)
    results.append({'evidence':path.name,'task':task,'checks':checks,'call_watermark':watermark,'database_calls':rows,'attempts':attempts})
report={'release':release,'at':time.time(),'passed':all(all(r['checks'].values()) for r in results),'results':results}
(root/'database-reconciliation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':report['passed'],'snapshots':len(results),'release':release['source']}))
if not report['passed']:raise SystemExit(1)
