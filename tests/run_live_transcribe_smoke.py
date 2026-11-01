"""One synthetic one-second audio request; no user media is uploaded."""
import json,math,os,shlex,struct,tempfile,wave
from pathlib import Path
from unittest.mock import patch
from webapp import agent_store,workflows
from webapp.store import Store
from webapp.agent_host import AgentHost
from webapp.execution_resources import IdentityPool


def main():
    config={}
    for line in Path('.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);parts=shlex.split(value);config[key]=parts[0] if parts else ''
    config.update(VIDEO_USE_TASK_MEMORY_BYTES=str(1024**3),VIDEO_USE_TASK_DISK_BYTES=str(64*1024**2),VIDEO_USE_TASK_BUDGET_SECONDS='300')
    with tempfile.TemporaryDirectory(prefix='vt-') as directory,patch.dict(os.environ,config):
        store=Store(Path(directory));agent_store.initialize(store);project=store.add_project('smoke','Transcription smoke')['id']
        source=store.project_dir(project)/'sources/tone.wav'
        with wave.open(str(source),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(b''.join(struct.pack('<h',int(8000*math.sin(i*.15))) for i in range(16000)))
        store.add_asset(project,'audio','synthetic tone','tone.wav',{'kind':'audio','duration':1,'audio_tracks':[{}]})
        plan=agent_store.add_turn(store,project,'plan','Test one synthetic audio request, without personal data.')
        agent_store.finish(store,plan,'succeeded','Approved one functional transcription request.')
        turn=agent_store.add_turn(store,project,'execute','Confirmed',plan_id=plan);store.execute("UPDATE agent_turns SET status='running' WHERE id=?",(turn,))
        workflows.submit(store,turn,{'key':'speech','name':'真实转写接口验证','steps':[{'id':'transcribe','name':'转写合成音频','kind':'operation','operation':'audio.transcribe','inputs':{'source':{'asset':'audio'}},'goal':'Validate the real transport and output contract, not speech accuracy','on_failure':'pause','acceptance':[{'kind':'json','required_keys':['text','words']}]}]})
        workflows.state(store,turn,'ready');host=AgentHost(store.root);host.identities=IdentityPool(store.root/'node',start=590000,size=4)
        lease=host.coordinator.claim(turn,'workflow');host.execute_claim(lease)
        step=workflows.get(store,turn)['steps'][0]
        result={'passed':step['status']=='succeeded','status':step['status'],'scope':'one-second synthetic tone; transport and JSON contract only','diagnostics':step['diagnostics'],
            'calls':store.query('SELECT operation,state,usage,cost FROM workflow_provider_calls')}
        Path('docs/test-evidence/live-transcribe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False))
        return not result['passed']

if __name__=='__main__':raise SystemExit(main())
