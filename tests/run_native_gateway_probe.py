"""Real gVisor/private-network/TLS probe; model handler is a no-cost fixture."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import socket
import time
import urllib.request
from types import SimpleNamespace
import uuid


PROBE='''import json,sys,socket,ssl,urllib.request,urllib.error
from pathlib import Path
value=json.load(sys.stdin)
Path('/state/ca.pem').write_text(value['ca'])
headers={'Authorization':'Bearer '+value['token'],'X-Video-Use-Attempt':value['attempt'],
 'X-Video-Use-Generation':'1','X-Video-Use-Sandbox':value['sandbox']}
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile='/state/ca.pem')))
def request(path,override=None):
 try:
  response=opener.open(urllib.request.Request(value['endpoint']+path,data=b'{}',headers={**headers,**(override or {})}),timeout=8)
  return response.status
 except urllib.error.HTTPError as exc:return exc.code
def connects(host,port):
 try:
  with socket.create_connection((host,port),timeout=2):return True
 except OSError:return False
print(json.dumps({'authorized':request('/v1/responses'),'foreign_token':request('/v1/responses',{'Authorization':'Bearer '+'x'*48}),
 'foreign_attempt':request('/v1/responses',{'X-Video-Use-Attempt':'foreign'}),'ungranted_route':request('/workflow/proposal'),
 'host_other_port':connects('172.31.0.1',value['blocked_port']),'peer_sandbox':connects(value['peer'],18770),'internet':connects('1.1.1.1',443)}))
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--docker',required=True);parser.add_argument('--image',required=True)
    parser.add_argument('--tls',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve()
    if not output.is_relative_to('/data2/wujing03'):raise ValueError('Probe evidence must stay on data2')
    from webapp.native_network import verify
    verify('172.31.0.1','172.31.0.0/24')
    docker=args.docker;owned=[];gateway=None;identifier=uuid.uuid4().hex[:12]
    blocked=socket.socket();blocked.bind(('172.31.0.1',0));blocked.listen(4)
    if 18880<=blocked.getsockname()[1]<=18949:raise ValueError('Probe listener unexpectedly overlaps model ports')
    try:
        for suffix in ('peer','client'):
            name='native-network-'+identifier+'-'+suffix
            subprocess.run([docker,'run','-d','--name',name,'--network','sandbox-agent-net','--runtime','runsc',
                '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','256m','--cpus','1',
                '--tmpfs','/state:rw,uid=10001,gid=10001,mode=700','--entrypoint','/opt/venv/bin/python',
                args.image,'-m','http.server','18770','--bind','0.0.0.0'],check=True,capture_output=True)
            owned.append(name)
        ips=[subprocess.check_output([docker,'inspect','--format','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',name],text=True).strip() for name in owned]
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline=time.monotonic()+10
        while True:
            try:
                with opener.open('http://'+ips[0]+':18770',timeout=2) as response:
                    if response.status!=200:raise AssertionError('Peer positive control is unavailable')
                break
            except OSError:
                if time.monotonic()>=deadline:raise
                time.sleep(.2)
        from webapp.native_gateway import NativeAttemptGateway
        from webapp.agent_profiles import AgentProfile
        from webapp.runtime_protocol import RuntimeIdentity
        from webapp.gateway_contracts import json_result
        os.environ['VIDEO_USE_MODEL_API_KEY']='probe-placeholder'
        profile=AgentProfile(base_url='https://provider.invalid')
        lease=SimpleNamespace(run_id=identifier,attempt=identifier,bind=nullcontext)
        tls=Path(args.tls)
        env={'VIDEO_USE_NODE_GATEWAY_BIND':'172.31.0.1','VIDEO_USE_NODE_GATEWAY_SUBNET':'172.31.0.0/24',
            'VIDEO_USE_NODE_GATEWAY_CERT':str(tls/'server.crt'),'VIDEO_USE_NODE_GATEWAY_KEY':str(tls/'server.key'),
            'VIDEO_USE_NODE_GATEWAY_CA':str(tls/'server.crt')}
        gateway=NativeAttemptGateway(lease,RuntimeIdentity(identifier,1,owned[1]),profile,ips[1],lambda value:value,environ=env)
        gateway.core.handlers={route:lambda body,permit:json_result(200,{'fixture':True}) for route in gateway.core.handlers}
        request={'endpoint':gateway.material.endpoint,'token':gateway.material.token,'attempt':identifier,
            'sandbox':owned[1],'peer':ips[0],'blocked_port':blocked.getsockname()[1],'ca':(tls/'server.crt').read_text()}
        raw=subprocess.check_output([docker,'exec','-i',owned[1],'/opt/venv/bin/python','-c',PROBE],
            input=json.dumps(request).encode(),timeout=45)
        result=json.loads(raw)
        expected={'authorized':200,'foreign_token':403,'foreign_attempt':403,'ungranted_route':403,
            'host_other_port':False,'peer_sandbox':False,'internet':False}
        if result!=expected:raise AssertionError(result)
        gateway.revoke()
        raw=subprocess.check_output([docker,'exec','-i',owned[1],'/opt/venv/bin/python','-c',PROBE],
            input=json.dumps(request).encode(),timeout=45)
        revoked=json.loads(raw)
        if revoked['authorized']!=403:raise AssertionError('Revoked grant was accepted')
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps({'passed':True,'scope':'real gVisor network/TLS; fixture business handler, no model requests',
            'checks':result,'revoked_status':revoked['authorized']},indent=2)+'\n')
        print(json.dumps({'passed':True,'checks':result,'revoked_status':revoked['authorized']}))
    finally:
        blocked.close()
        if gateway:gateway.close()
        for name in owned:subprocess.run([docker,'rm','-f',name],check=True,capture_output=True)


if __name__=='__main__':main()
