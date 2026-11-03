"""Explicit live model smoke. Loads local service configuration without printing it."""
import json
import os
from pathlib import Path
import shlex
import tempfile
from unittest.mock import patch
from webapp import agent_store,agent_sandbox,workflows
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.execution_resources import IdentityPool


def main():
    configuration={}
    for name in ('.env','.agent.env'):
        for line in Path(name).read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:continue
            key,value=line.split('=',1);parts=shlex.split(value)
            configuration[key.strip()]=parts[0] if parts else ''
    from urllib.parse import urlsplit
    configured_host=urlsplit(configuration.get('VIDEO_USE_MODEL_BASE_URL','')).hostname
    configuration['VIDEO_USE_INTERNAL_PROVIDER_HOSTS']='localhost,127.0.0.1,::1,'+(configured_host or '')
    configuration.update(VIDEO_USE_TASK_MEMORY_BYTES=str(2*1024**3),VIDEO_USE_TASK_DISK_BYTES=str(128*1024**2),VIDEO_USE_TASK_MODEL_CALLS='12',VIDEO_USE_TASK_BUDGET_SECONDS='300')
    with tempfile.TemporaryDirectory(prefix='vr-') as directory,patch.dict(os.environ,configuration):
        store=Store(Path(directory));agent_store.initialize(store)
        project=store.add_project('test-owner','Live review smoke')['id']
        plan=agent_store.add_turn(store,project,'plan','生成一张 16x16 纯蓝色图片，仅作功能测试。')
        agent_store.finish(store,plan,'succeeded','已确认：使用本地 Pillow 生成 16x16 RGB 纯蓝色图片，并查看实际结果后交付。无需调用图片生成模型。')
        turn=agent_store.add_turn(store,project,'execute','按已确认方案执行并检查。',plan_id=plan)
        store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
        env=agent_sandbox.prepare(store,project,'test','execute',turn)
        script=env['workspace']/'edit/make.py';script.write_text("import os\nfrom pathlib import Path\nfrom PIL import Image\nImage.new('RGB',(16,16),'blue').save(Path(os.environ['VIDEO_USE_STEP_OUTPUT'])/'final.png')\n");script.chmod(0o644)
        workflows.submit(store,turn,{'key':'live','name':'生成并验收蓝色图片','steps':[{'id':'make','name':'生成蓝色图片','kind':'script','argv':['python','edit/make.py'],'outputs':['final.png'],'goal':'16x16 纯蓝色图片','on_failure':'diagnose','acceptance':[{'kind':'image','width':16,'height':16},{'kind':'agent','criterion':'实际查看图像，确认整幅图为蓝色且没有额外内容'}]}]})
        workflows.state(store,turn,'ready')
        host=AgentHost(store.root);host.identities=IdentityPool(store.root/'node',start=580000,size=4)
        for _ in range(8):
            row=agent_store.turn(store,turn)
            if row['status'] in ('succeeded','failed','cancelled','paused'):break
            kind='workflow' if row['workflow']['status'] in ('ready','staged','cancelling') else 'agent'
            lease=host.coordinator.claim(turn,kind)
            if not lease:raise RuntimeError('Smoke could not claim task')
            host.execute_claim(lease)
        row=agent_store.turn(store,turn)
        result={'status':row['status'],'steps':[{'id':s['id'],'status':s['status']} for s in row['workflow']['steps']],
                'agent_reviews':store.query('SELECT purpose,status,runs FROM workflow_agent_reviews'),
                'agent_text':row['text'],'tools':store.query("SELECT payload FROM agent_events WHERE run_id=? AND kind IN ('tool','tool_output') ORDER BY seq DESC LIMIT 6",(turn,)),'artifacts':len(row['artifacts']),'error':row['error'],'diagnostics':row['workflow']['steps'][0]['diagnostics'],
                'last_events':store.query("SELECT kind,payload FROM agent_events WHERE run_id=? AND kind='progress' ORDER BY seq DESC LIMIT 4",(turn,))}
        if row['status']!='succeeded':
            checkpoint=store.query('SELECT path FROM execution_checkpoints WHERE project=?',(project,))
            outputs=[]
            if checkpoint:
                for path in (store.root/checkpoint[0]['path']/'state').rglob('*.jsonl'):
                    for line in path.read_text(errors='replace').splitlines():
                        if 'function_call_output' in line or 'tool_result' in line:
                            outputs.append(host.redact(line)[-2000:])
            result['tool_errors']=outputs[-8:]
        Path('docs/live-workflow-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False))
        if row['status']!='succeeded':raise SystemExit(1)

if __name__=='__main__':main()
