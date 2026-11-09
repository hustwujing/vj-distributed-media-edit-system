"""Run inside an offline Runtime image: real SDK, fixture model HTTP only."""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from webapp.harness_adapters import get
from webapp.runtime_harness import StdioHarnessBridge,HarnessController
from webapp.runtime_gateway import GatewayClientServer


def main():
    harness=sys.argv[1];paths=[];events=[]
    model='claude-sonnet-5-5' if harness=='claude_code' else 'deepseek-v4.1-flash'
    if harness=='codex':raise ValueError('Responses fixture must be added before testing Codex')
    class Response:
        status_code=200;closed=False
        headers={'Content-Type':'text/event-stream'}
        def __init__(self,data):self.data=data
        def iter_content(self,chunk_size):return iter((self.data,))
        def close(self):self.closed=True
    class Client:
        def forward(self,method,path,headers,raw):
            paths.append(path);body=json.loads(raw)
            message={'id':'msg_probe','type':'message','role':'assistant','model':model,'content':[],
                'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}
            frames=[{'type':'message_start','message':message},
                {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Probe completed.'}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':4}},
                {'type':'message_stop'}]
            if not body.get('stream'):
                message.update(content=[{'type':'text','text':'Probe completed.'}],stop_reason='end_turn',usage={'input_tokens':10,'output_tokens':4})
                response=Response(json.dumps(message).encode());response.headers={'Content-Type':'application/json'};return response
            return Response(''.join('event: '+frame['type']+'\ndata: '+json.dumps(frame)+'\n\n' for frame in frames).encode())
        def close(self):pass
    server=GatewayClientServer(Client(),port=18766);server.start()
    for path in ('/state/tmp','/state/claude','/state/dsh'):Path(path).mkdir(parents=True,exist_ok=True)
    config=Path('/run/video-use/harness-config.json');config.write_text(json.dumps({'model':model,'reasoning_effort':'high','max_output_tokens':2048}))
    adapter=get(harness)
    bridge=StdioHarnessBridge(adapter,adapter.runtime_launch(str(config)),environment={'PYTHONPATH':'/opt/harness:/opt/video-use:/opt/video-use/helpers'})
    controller=HarnessController(adapter,SimpleNamespace(model=model,reasoning_effort='high'),bridge,events.append,{'probe':True})
    try:
        controller.dispatch('runtime.initialize',{'probe':True})
        session=controller.dispatch('session.open',{'instructions':'Return one short acknowledgement. No tools required.'})['session_id']
        controller.dispatch('turn.start',{'session_id':session,'text':'Protocol fixture probe.'})
        deadline=time.monotonic()+60
        while not any(event.kind=='turn.ended' for event in events) and time.monotonic()<deadline:time.sleep(.1)
        ended=next((event.data for event in reversed(events) if event.kind=='turn.ended'),None)
        result={'harness':harness,'model_handler':'fixture','external_network':'none','paths':paths,'ended':ended,
            'public_text':[event.data.get('text') for event in events if event.kind=='message.completed']}
        print(json.dumps(result),flush=True)
        if not ended or ended.get('status')!='completed':raise RuntimeError('Native SDK wire fixture failed')
    finally:controller.close();server.close()


if __name__=='__main__':main()
