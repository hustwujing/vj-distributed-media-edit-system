"""Live service protocol check with a disposable local ledger, never production tasks."""
import json,os,tempfile,time,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deploy.cluster import environment
os.environ.update(environment())
os.environ.update(VIDEO_USE_DATABASE_URL='',VIDEO_USE_DISTRIBUTED='0',VIDEO_USE_OBJECT_BUCKET='')
from webapp.store import Store
from webapp import agent_store,plan_readiness
from webapp.task_leases import Coordinator
from webapp.task_usage import snapshot
from webapp.agent_host import AgentHost
from webapp.semantic_review import classify
results=[]
with tempfile.TemporaryDirectory(prefix='vu-conversation-live-') as folder:
 store=Store(Path(folder));agent_store.initialize(store)
 for message in ('你好啊','这个项目现在有几个素材？','请制定可执行方案：调用文生图生成1024×1024的水彩海边小屋，再用本地脚本裁成512×512 PNG，交付原图及裁切版。只生成一次，缺省参数请合理选择，无需再问风格问题。等我确认后执行。'):
  p=store.add_project('qa','protocol')['id'];t=agent_store.add_turn(store,p,'plan',message);plan_readiness.register(store,t)
  host=object.__new__(AgentHost);host.store=store;host.model=os.environ['VIDEO_USE_MODEL'];host.base_url=os.environ['VIDEO_USE_MODEL_BASE_URL'];host.key=os.environ['VIDEO_USE_MODEL_API_KEY']
  host.identities=SimpleNamespace(node='probe-node',root=Path(folder)/'identity');host.stop=threading.Event();host.active={};host.lock=threading.Lock()
  lease=Coordinator(store,'probe-node','qa').claim(t,'agent');start=time.monotonic()
  with patch('webapp.agent_host.TaskRuntime',side_effect=AssertionError('Protocol probe must not allocate runtime')):host._execute_claim(lease)
  row=agent_store.turn(store,t);u=snapshot(store,t);results.append({'message':message,'status':row['status'],'text':row['text'],'route':row['routing'],'seconds':round(time.monotonic()-start,3),'calls':u['counts'],'tokens':u['tokens']})
  print(json.dumps(results[-1],ensure_ascii=False),flush=True)
 for purpose,state in [('plan',{'user_request':'请将我的图片缩小成64×64 PNG。','proposal':'使用素材a整张图片，用Pillow等比缩放并居中裁成64×64 PNG，交付small.png并检查尺寸与可读性。保留原图，失败时停止；预计20秒，不调用外部服务。等待用户批准执行。','intent':{'goal':'缩小为64×64PNG','operations':['script'],'estimated_seconds':20},'assets':[{'id':'a','kind':'image'}]}),('confirmation',{'message':'就按这个方案做吧','proposal':'将图片缩小成64×64 PNG','intent':{'operations':['script']}}),('confirmation',{'message':'可以，但是背景改为蓝色','proposal':'将图片缩小成64×64 PNG','intent':{'operations':['script']}})]:
  p=store.add_project('qa','semantic')['id'];t=agent_store.add_turn(store,p,'plan','semantic probe')
  try:decision=classify(store,t,purpose,state);result={'purpose':purpose,'decision':decision}
  except Exception as exc:result={'purpose':purpose,'error':type(exc).__name__}
  results.append(result);print(json.dumps(result),flush=True)
Path('/tmp/vu-conversation-live-probe.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
