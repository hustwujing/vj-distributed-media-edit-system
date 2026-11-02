"""Unprivileged HTTP/model broker; no ability to launch host commands as root."""
import base64
import ctypes
import json
import os
from pathlib import Path
import resource
import sys
import time
from .store import Store
from .task_leases import Lease
from .gateway_process import Client
from .agent_gateway import Gateway
from . import workflows,agent_store
from .workflow_review import dispatch as workflow_dispatch
from .image_canvas import agent_patch


def main():
    from .telemetry import setup,CONTEXT
    config=json.load(sys.stdin)
    CONTEXT.set({'task_id':config['lease']['turn_id'],'attempt_id':config['lease']['attempt'],'worker':config['lease']['worker']})
    setup('gateway')
    if os.getuid()==0:raise SystemExit('Gateway service must be unprivileged')
    ctypes.CDLL(None).prctl(1,15,0,0,0)  # SIGTERM when coordinator exits.
    if os.getppid()!=config['parent']:return
    resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
    resource.setrlimit(resource.RLIMIT_NOFILE,(1024,1024))
    os.environ['ELEVENLABS_API_KEY']=config.get('eleven_key','')
    for name,value in config.get('storage_credentials',{}).items():
        if name not in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION','VIDEO_USE_OBJECT_ENCRYPTION_KEY'):
            raise ValueError('Unexpected broker storage credential')
        os.environ[name]=value
    store=Store(Path(config['root']));lease=Lease(store=store,**config['lease'])
    if sys.argv[1]=='relay':
        from .provider_relay import ImageRelay
        from .image_document import ImageTask
        relay=ImageRelay(Path(config['path']),ImageTask.model_validate(config['task']),lease,config['uid'],config['gid'],
                         listener_fd=int(sys.argv[2]),call_id=config['call'],base_override=config['base'])
        print('READY',flush=True)
        while os.getppid()==config['parent']:
            time.sleep(1)
            try:lease.check()
            except Exception:return
        return
    os.environ['VIDEO_USE_MODEL_BASE_URL']=config['base_url']
    os.environ['VIDEO_USE_MODEL_API_KEY']=config['key']
    if config.get('image_generation_key'):os.environ['VIDEO_USE_IMAGE_GENERATION_KEY']=config['image_generation_key']
    if config.get('image_video_key'):os.environ['VIDEO_USE_IMAGE_VIDEO_KEY']=config['image_video_key']
    if config.get('generation_key'):os.environ['VIDEO_USE_GENERATION_KEY']=config['generation_key']
    client=Client(int(sys.argv[2]))
    def extract(filename,track,destination):
        asset=next(a for a in config['assets'] if a['filename']==filename)
        result=client.request('extract',{'asset':asset['id'],'track':track})
        destination.write_bytes(base64.b64decode(result['audio'],validate=True))
    def notify(kind,payload):agent_store.event(store,lease.turn_id,kind,payload)
    with lease.bind():
        from .agent_profiles import AgentProfile
        gateway=Gateway(Path(config['socket']),Path(config['project']),config['assets'],config['mode'],config['model'],
            config['base_url'],config['key'],config['uid'],config['gid'],notify,extract,
            canvas_callback=(lambda body,asset:agent_patch(store,Path(config['project']).name,lease.turn_id,body,asset)) if config['canvas'] else None,
            workflow_callback=(lambda body:client.request('workflow_preflight',body) if body.get('action')=='preflight' else workflow_dispatch(store,lease.turn_id,body,config['mode'],config.get('workflow_review_id'))) if config['workflow'] else None,
            listener_fd=int(sys.argv[1]),broker_client=client,
            profile=AgentProfile(**config['agent_profile']) if config.get('agent_profile') else None)
        gateway.thread.start()
    print('READY',flush=True)
    while os.getppid()==config['parent']:
        time.sleep(1)
        try:lease.check()
        except Exception:return


if __name__=='__main__':main()
