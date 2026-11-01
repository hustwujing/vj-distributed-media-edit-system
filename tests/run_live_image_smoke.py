"""One real text-to-image request through the project's configured model gateway."""
import json
import os
from pathlib import Path
import shlex
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch
from webapp import agent_store,workflows
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.execution_resources import IdentityPool

config={}
for line in Path('.agent.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key,value=line.split('=',1);parts=shlex.split(value);config[key]=parts[0] if parts else ''
config['VIDEO_USE_INTERNAL_PROVIDER_HOSTS']='localhost,127.0.0.1,::1,'+urlsplit(config['VIDEO_USE_MODEL_BASE_URL']).hostname
config['VIDEO_USE_IMAGE_GENERATION_MODEL']='gpt-image-1.5'
config.update(VIDEO_USE_TASK_MEMORY_BYTES=str(2*1024**3),VIDEO_USE_TASK_DISK_BYTES=str(128*1024**2),VIDEO_USE_TASK_BUDGET_SECONDS='300')
with tempfile.TemporaryDirectory(prefix='vi-') as folder,patch.dict(os.environ,config):
    store=Store(Path(folder));agent_store.initialize(store);project=store.add_project('smoke','Text to image smoke')['id']
    plan=agent_store.add_turn(store,project,'plan','Generate a simple blue circle icon for an integration test.')
    agent_store.finish(store,plan,'succeeded','Approved one text-to-image call, 1024x1024, low quality with explicitly approved contain resizing for functional validation.')
    turn=agent_store.add_turn(store,project,'execute','Confirmed',plan_id=plan)
    store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
    workflows.submit(store,turn,{'key':'live-image','name':'文生图集成验证','steps':[{'id':'generate','name':'生成简单图标','kind':'operation','operation':'image.generate_from_text',
        'parameters':{'prompt':'A simple solid blue circle centered on a plain white background. Flat geometric icon. No text, no gradients.','width':1024,'height':1024,'quality':'low','fit':'contain'},
        'goal':'Generate a real image without any input image','on_failure':'pause','timeout':240,'acceptance':[{'kind':'image','width':1024,'height':1024}]}]})
    workflows.state(store,turn,'ready')
    host=AgentHost(store.root);host.identities=IdentityPool(store.root/'node',start=490000,size=4)
    lease=host.coordinator.claim(turn,'workflow');host.execute_claim(lease)
    w=workflows.get(store,turn);step=w['steps'][0]
    result={'passed':step['status']=='succeeded','step_status':step['status'],'files':step['quality'].get('files',[]),'diagnostics':step['diagnostics'],
        'calls':store.query('SELECT operation,state,usage,cost FROM workflow_provider_calls')}
    Path('docs/test-evidence').mkdir(exist_ok=True)
    artifacts=agent_store.turn(store,turn)['artifacts']
    if artifacts:
        import shutil
        shutil.copyfile(store.project_dir(project)/'edit/agent_exports'/turn/artifacts[0]['file'],'docs/test-evidence/live-generated.png')
    Path('docs/test-evidence/live-image.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
    if not result['passed']:raise SystemExit(1)
