import sys,json
from pathlib import Path
from campaign_probe import store
s=store();root=Path('.cluster/campaigns/20261101-combined');state=json.loads((root/'stream-state.json').read_text());tasks=[state['knowledge'],state['analysis']];result={}
for table,column,values,order in [('agent_turns','id',tasks,'id'),('usage_calls','task_id',tasks,'seq'),('agent_routes','task_id',tasks,'task_id'),('execution_activities','task_id',tasks,'id'),('agent_sessions','project',[state['project']],'project'),('execution_checkpoints','project',[state['project']],'project')]:
 result[table]=s.query('SELECT * FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in values)+') ORDER BY '+order,tuple(values))
result['demo_library']=s.query('SELECT * FROM demo_library')
p=root/('snapshot-'+sys.argv[1]+'.json');p.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
if sys.argv[1]=='after-restart':assert json.loads((root/'snapshot-before-restart.json').read_text())==result
print('Snapshot',sys.argv[1],'verified; activities',len(result['execution_activities']),'calls',len(result['usage_calls']))
