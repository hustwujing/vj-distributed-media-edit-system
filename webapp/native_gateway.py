"""Attempt-private TLS listener reusing the fenced, metered model gateway.

Bearer grants deliberately live only with this listener/Attempt. They cannot be
resumed after executor death; a new generation gets a new port, token and grant.
Business budgets and usage remain in the shared database, not this transport.
"""
import ipaddress
import errno
import base64
import json
import os
from pathlib import Path
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

from .agent_gateway import Gateway
from .gateway_business import GatewayBusinessCore
from .node_gateway import (GrantAuthority,InMemoryGrantStore,NodeGatewayAuthorizer,
    NodeGatewayCore,GatewayAuthorizationError,GatewayBudgetError)
from .runtime_injection import GatewayGrant
from .gateway_contracts import json_result


class NativeImageGenerationGateway:
    """Narrow OpenAI-compatible image route backed by host credentials."""
    ROUTE='/v1/images/generations'

    def __init__(self,lease):self.lease=lease

    def handle(self,body,permit):
        value=GatewayBusinessCore.decode(body)
        allowed={'model','prompt','n','size','quality','background','output_format','output_compression','moderation'}
        if set(value)-allowed:raise ValueError('Unsupported image generation request field')
        if value.get('n',1)!=1 or value.get('output_format','png')!='png':
            raise ValueError('Native image generation supports one PNG per call')
        size=value.get('size','1280x720')
        if size=='auto':width,height=1280,720
        else:
            import re
            match=re.fullmatch(r'([1-9][0-9]{1,3})x([1-9][0-9]{1,3})',str(size))
            if not match:raise ValueError('Invalid image generation size')
            width,height=map(int,match.groups())
        quality=value.get('quality','low')
        if quality not in ('low','medium','high','auto','xhigh','max'):
            raise ValueError('Invalid image generation quality')
        from .workflow_gateway import context
        run,task,_=context(self.lease)
        operation='image.generate_from_text'
        declared=set((task['spec'].get('runtime_requirements') or {}).get('gateway_operations') or ())
        if operation not in declared:raise ValueError('Image generation is outside the frozen Task Gateway capabilities')
        limit=run['authorization']['provider_call_budget'].get(operation,0)
        if limit<=0:raise ValueError('Image generation has no frozen provider budget')
        self.lease.consume('provider:'+operation,limit)
        from .image_generation import generate,configuration
        raw,metadata=generate({'prompt':value.get('prompt',''),'width':width,'height':height,
            'quality':'high' if quality in ('xhigh','max') else quality,'background':'#ffffff','fit':'native'})
        _,_,model=configuration()
        from .telemetry import emit
        emit('native.gateway_image_generated','INFO',task_id=self.lease.run_id,attempt_id=self.lease.attempt,
             operation=operation,bytes=len(raw),width=width,height=height)
        return json_result(200,{'created':int(time.time()),'model':model,
            'data':[{'b64_json':base64.b64encode(raw).decode()}],
            'usage':metadata.get('usage'),'cost':metadata.get('cost')})


class NativeModelGateway:
    # Reuse the actual model adapter, lease checks, budget reservation and
    # receipt parser. The native transport exposes no media or graph mutation.
    forward_result=Gateway.forward_result
    normalize_native_images=Gateway.normalize_native_images
    note_model_unavailable=Gateway.note_model_unavailable
    dynamic_task=True
    broker=None
    workflow_callback=None
    canvas_callback=None

    def __init__(self,lease,profile,normalize):
        from .model_adapters import get
        self.lease=lease;self.profile=profile;self.model=profile.model
        self.base_url,self.key=profile.credentials()
        self.model_adapter=get(profile.protocol)
        self.response_transport=profile.native_response_transport
        self.lock=threading.Lock();self.calls=0;self.normalize=normalize

    def check(self):
        from .workflow_gateway import context
        context(self.lease)

    def normalize_model_images(self,body):
        nodes=[body]
        while nodes:
            value=nodes.pop()
            if isinstance(value,list):nodes.extend(value)
            elif isinstance(value,dict):
                if value.get('type')=='input_image':
                    raw=value['image_url'].split(';base64,',1)[1]
                    value['image_url']='data:image/jpeg;base64,'+self.normalize(raw)
                else:nodes.extend(value.values())


class NativeAttemptGateway:
    def __init__(self,lease,identity,profile,source_ip,normalize,*,public_urls=(),gateway_operations=(),environ=None):
        env=os.environ if environ is None else environ
        address=env.get('VIDEO_USE_NODE_GATEWAY_BIND','')
        parsed=ipaddress.ip_address(address)
        if not parsed.is_private or parsed.is_unspecified or parsed.is_loopback:
            raise ValueError('Native Gateway must bind the private sandbox bridge address')
        network=ipaddress.ip_network(env.get('VIDEO_USE_NODE_GATEWAY_SUBNET',''),strict=True)
        if ipaddress.ip_address(source_ip) not in network or parsed not in network:
            raise ValueError('Sandbox source is outside the configured internal bridge')
        certificate=env.get('VIDEO_USE_NODE_GATEWAY_CERT','')
        key=env.get('VIDEO_USE_NODE_GATEWAY_KEY','')
        ca=Path(env.get('VIDEO_USE_NODE_GATEWAY_CA','')).read_bytes()
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_2;context.load_cert_chain(certificate,key)
        self.lease=lease;self.closed=False;self.last_error=None
        self.grants=InMemoryGrantStore();authority=GrantAuthority(self.grants)
        business=GatewayBusinessCore(NativeModelGateway(lease,profile,normalize))
        routes=({'/responses','/v1/responses'} if profile.protocol=='responses' else
                {'/messages','/v1/messages'})
        handlers={route:(lambda body,permit:business.model(business.decode(body))) for route in routes}
        unsupported=set(gateway_operations)-{'image.generate_from_text'}
        if unsupported:raise ValueError('Unsupported Native Gateway operations: '+', '.join(sorted(unsupported)))
        if 'image.generate_from_text' in gateway_operations:
            image=NativeImageGenerationGateway(lease);routes.add(image.ROUTE);handlers[image.ROUTE]=image.handle
        if public_urls:
            from .public_http_gateway import PublicHTTPGateway,ROUTE
            routes.add(ROUTE);handlers[ROUTE]=PublicHTTPGateway(lease,public_urls).handle
        self.core=NodeGatewayCore(NodeGatewayAuthorizer(self.grants),handlers)
        self.issued=authority.issue(task_id=lease.run_id,attempt_id=identity.attempt_id,
            generation=identity.generation,sandbox_id=identity.sandbox_id,source_ip=source_ip,
            routes=routes,expires_at=time.time()+min(profile.budget['run_seconds'],3600)+60,
            max_calls=profile.budget['run_models'],max_request_bytes=256*1024**2,
            max_response_bytes=256*1024**2)
        outer=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def setup(self):
                self.request.settimeout(5)
                self.request.do_handshake()
                super().setup()
            def do_GET(self):self.send_error(405)
            def do_POST(self):
                result=None;started=False
                self.connection.settimeout(30)
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=32*1024**2 or self.headers.get('Transfer-Encoding'):
                        self.send_error(413);return
                    body=self.rfile.read(size)
                    if len(body)!=size:raise ValueError('Truncated request')
                    with lease.bind():
                        result=outer.core.dispatch(dict(self.headers),self.path,self.client_address[0],body)
                        self.send_response(result.status)
                        self.send_header('Content-Type',result.headers.get('Content-Type','application/json'))
                        self.send_header('Connection','close');self.end_headers();started=True
                        for chunk in result:self.wfile.write(chunk);self.wfile.flush()
                except (GatewayAuthorizationError,GatewayBudgetError,ValueError) as exc:
                    if isinstance(exc,ValueError):
                        outer.last_error=exc
                        from .telemetry import emit
                        emit('native.gateway_business_rejected','ERROR',exc=exc,attempt_id=lease.attempt)
                    if not started:self.send_error(403 if isinstance(exc,GatewayAuthorizationError) else 429 if isinstance(exc,GatewayBudgetError) else 422)
                except (BrokenPipeError,ConnectionResetError):pass
                except Exception as exc:
                    outer.last_error=exc
                    from .telemetry import emit
                    emit('native.gateway_failed','ERROR',exc=exc,attempt_id=lease.attempt)
                    if not started:self.send_error(502,'Model gateway request failed')
                finally:
                    if result:result.close()
                    self.close_connection=True
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def __init__(self,*args):
                self.slots=threading.BoundedSemaphore(4);super().__init__(*args)
            def process_request(self,request,client_address):
                if not self.slots.acquire(False):self.shutdown_request(request);return
                try:super().process_request(request,client_address)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,*args):
                try:super().process_request_thread(*args)
                finally:self.slots.release()
        # A fixed small range lets deployment firewall this bridge without
        # granting arbitrary access to the host's other listening services.
        self.server=None
        for port in range(18880,18950):
            try:self.server=Server((address,port),Handler);break
            except OSError as exc:
                if exc.errno!=errno.EADDRINUSE:raise
        if self.server is None:raise RuntimeError('Native Gateway listener capacity exhausted')
        try:self.server.socket=context.wrap_socket(self.server.socket,server_side=True,do_handshake_on_connect=False)
        except BaseException:self.server.server_close();raise
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True,name='attempt-model-gateway')
        self.thread.start()
        self.material=GatewayGrant(f'https://{address}:{self.server.server_port}',self.issued.token,ca,tuple(sorted(routes)))

    def revoke(self):self.grants.revoke(self.issued.id)

    def close(self):
        self.revoke()
        if self.closed:return
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        self.closed=True
