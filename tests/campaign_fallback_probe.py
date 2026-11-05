"""Real model entry routing, with disposable metadata and no task sandbox."""
import os,json,tempfile,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deploy.dynamic_cluster import environment
os.environ.update(environment());os.environ.update(VIDEO_USE_DATABASE_URL='',VIDEO_USE_DISTRIBUTED='0',VIDEO_USE_OBJECT_BUCKET='')
from webapp import agent_store,plan_readiness
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.task_leases import Coordinator
from webapp.task_usage import snapshot
cases=[('capability-check','请核实当前系统实际有没有字幕转双语和导出剪辑工程的能力。需要查阅实际工具说明后再回答，不要凭常识猜，也不要开始制作。','general_analysis'),
       ('investigate-project','我不知道当前项目该从哪里继续。请综合核对项目记录、已有文件和实际可用工具，调查后说明还缺哪些条件。先分析，不修改或生成文件。','general_analysis'),
       ('greeting','你好啊','light'),('clarification','我想剪一条视频，但还没决定主题，也没上传素材。你先问我最关键的问题。','light')]
results=[]
with tempfile.TemporaryDirectory() as folder:
 s=Store(Path(folder));agent_store.initialize(s)
 for name,prompt,expected in cases:
  p=s.add_project('qa','fallback-probe')['id'];t=agent_store.add_turn(s,p,'plan',prompt);plan_readiness.register(s,t)
  host=object.__new__(AgentHost);host.store=s;host.model=os.environ['VIDEO_USE_MODEL'];host.base_url=os.environ['VIDEO_USE_MODEL_BASE_URL'];host.key=os.environ['VIDEO_USE_MODEL_API_KEY'];host.identities=SimpleNamespace(node='probe-node',root=Path(folder)/'identity');host.stop=threading.Event();host.active={};host.lock=threading.Lock()
  lease=Coordinator(s,'probe-node','qa').claim(t,'agent')
  with patch('webapp.agent_host.TaskRuntime',side_effect=AssertionError('Light entry allocated runtime')):host._execute_claim(lease)
  row=agent_store.turn(s,t);u=snapshot(s,t)
  passed=(row['status']=='succeeded' and row['routing']['stage']=='light') if expected=='light' else (row['status']=='queued' and row['routing']['capability']==expected)
  result={'case':name,'prompt':prompt,'expected':expected,'passed':passed,'status':row['status'],'mode':row['mode'],'routing':row['routing'],'reason':plan_readiness.route(s,t)['reason'],'text':row['text'],'usage':u};results.append(result);print(json.dumps({k:result[k] for k in ('case','passed','status','routing','reason')},ensure_ascii=False),flush=True)
root=Path('.cluster/campaigns/20261101-fallback');(root/'real-model-probe.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
assert all(r['passed'] for r in results)
