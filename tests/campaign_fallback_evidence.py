"""Read-only production evidence for generic handoff and restart persistence."""
import json,sys
from pathlib import Path
from campaign_probe import store
from webapp.diagnostics import diagnose
from webapp.task_usage import snapshot
s=store();root=Path('.cluster/campaigns/20261101-fallback');state=json.loads((root/'state.json').read_text());tasks=[state['greeting'],state['task']]
if len(sys.argv)>1:
 result={}
 for table,column,values,order in [('agent_turns','id',tasks,'id'),('usage_calls','task_id',tasks,'seq'),('agent_routes','task_id',tasks,'task_id'),('agent_sessions','project',[state['project']],'project'),('execution_checkpoints','project',[state['project']],'project')]:
  result[table]=s.query('SELECT * FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in values)+') ORDER BY '+order,tuple(values))
 target=root/('snapshot-'+sys.argv[1]+'.json');target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');target.chmod(0o600)
 if sys.argv[1]=='after-restart':assert json.loads((root/'snapshot-before-restart.json').read_text())==result
 print('Snapshot verified:',sys.argv[1]);raise SystemExit
reports=[]
for name,task in [('greeting',state['greeting']),('fallback',state['task'])]:
 attempts=s.query('SELECT a.id,a.status,a.runtime,a.uid,a.started,a.finished,w.node,w.release FROM execution_attempts a LEFT JOIN execution_workers w ON w.id=a.worker WHERE a.run_id=? ORDER BY a.started',(task,))
 route=s.query('SELECT * FROM agent_routes WHERE task_id=?',(task,))[0];u=snapshot(s,task,limit=200)
 if name=='greeting':assert all(not x['runtime'] and x['uid'] is None for x in attempts)
 else:
  assert len(attempts)==2 and not attempts[0]['runtime'] and attempts[1]['runtime']
  assert route['capability']=='general_analysis' and u['run_id']==task and u['provider']['submissions']==0
  assert s.query("SELECT mode FROM agent_turns WHERE id=?",(task,))[0]['mode']=='plan'
  tools=s.query("SELECT c.operation FROM usage_calls c JOIN execution_attempts a ON a.id=c.attempt_id WHERE c.task_id=? AND c.category='tool' AND a.runtime<>''",(task,));assert tools
 diagnostic=diagnose(s,task,0,200);(root/(name+'-diagnostics.json')).write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2)+'\n')
 reports.append({'case':name,'task':task,'attempts':attempts,'route':route,'usage':u,'diagnostic_complete':diagnostic['complete'],'logs':len(diagnostic['logs']),'handoff_events':[log for log in diagnostic['logs'] if log.get('event')=='conversation.handoff']})
assert reports[1]['handoff_events'],'Handoff trace is missing'
(root/'database-evidence.json').write_text(json.dumps({'passed':True,'reports':reports},ensure_ascii=False,indent=2)+'\n');print('Database, permissions, attempts, usage and handoff traces verified')
