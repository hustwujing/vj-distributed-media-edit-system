"""Actual-model routing check; disposable project metadata, no edit runtime."""
import os,json,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deploy.cluster import environment
os.environ.update(environment());os.environ.update(VIDEO_USE_DATABASE_URL='',VIDEO_USE_DISTRIBUTED='0',VIDEO_USE_OBJECT_BUCKET='')
from webapp import agent_store,plan_readiness
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.task_leases import Coordinator
results=[]
with tempfile.TemporaryDirectory() as folder:
 s=Store(Path(folder));agent_store.initialize(s)
 for filename,prompt in [('rotated_90.png','请实际打开之前交付的旋转后PNG，确认它的尺寸，并描述主体与背景颜色。这次只读检查，不修改任何文件，也不要制定制作方案。'),('final_mix.wav','请实际听一下之前合成的音频，检查末尾有没有突然截断。不修改文件。')]:
  p=s.add_project('qa','saved-output')['id'];s.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?,?)',(p,'fixture','saved',json.dumps({'workspace':{'edit/'+filename:{'size':1000}},'state':{}}),s.now()))
  t=agent_store.add_turn(s,p,'plan',prompt);plan_readiness.register(s,t)
  host=object.__new__(AgentHost);host.store=s;host.model=os.environ['VIDEO_USE_MODEL'];host.base_url=os.environ['VIDEO_USE_MODEL_BASE_URL'];host.key=os.environ['VIDEO_USE_MODEL_API_KEY'];host.identities=SimpleNamespace(node='probe-node',root=Path(folder)/'identity');host.stop=threading.Event();host.active={};host.lock=threading.Lock()
  lease=Coordinator(s,'probe-node','qa').claim(t,'agent')
  with patch('webapp.agent_host.TaskRuntime',side_effect=AssertionError('Must commit handoff without a sandbox')):host._execute_claim(lease)
  row=agent_store.turn(s,t);assert row['status']=='queued' and row['routing']['capability']=='inspect_media',(filename,row['text'],row['routing'])
  results.append({'file':filename,'status':row['status'],'mode':row['mode'],'routing':row['routing'],'passed':True})
print(json.dumps(results,ensure_ascii=False));Path('.cluster/campaigns/20261101-conversation/saved-outputs-live-probe.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
