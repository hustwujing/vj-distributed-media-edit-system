"""Fixed-route sandbox client for the host Node Capability Gateway."""
from __future__ import annotations

from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading

import requests

from .runtime_protocol import RuntimeIdentity


class GatewayClientError(ValueError):
    pass


class GatewayClient:
    HEADER_ALLOWLIST={'content-type','accept','anthropic-version','openai-beta'}

    def __init__(self,endpoint,token,identity,allowed_routes,ca_file,*,session=None):
        parsed=urlsplit(endpoint)
        if (parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path not in ('','/')):
            raise GatewayClientError('Node Gateway endpoint must be a fixed HTTPS origin')
        if not isinstance(token,str) or not 32<=len(token)<=512 or not isinstance(identity,RuntimeIdentity):
            raise GatewayClientError('Invalid Gateway identity or token')
        routes=set(allowed_routes)
        if (not routes or len(routes)>64 or any(not isinstance(path,str) or
                not path.startswith('/') or urlsplit(path).path!=path or '//' in path for path in routes)):
            raise GatewayClientError('Invalid Gateway route grant')
        self.endpoint=endpoint.rstrip('/');self.token=token;self.identity=identity
        self.allowed_routes=routes;self.ca_file=ca_file
        self.session=session or requests.Session();self.session.trust_env=False

    def forward(self,method,path,headers,body):
        if method!='POST' or path not in self.allowed_routes:
            raise GatewayClientError('Gateway route is not granted')
        if not isinstance(body,(bytes,bytearray)) or not 0<len(body)<=32*1024*1024:
            raise GatewayClientError('Invalid Gateway request body')
        outgoing={name:value for name,value in headers.items()
                  if name.lower() in self.HEADER_ALLOWLIST or name.lower().startswith('x-stainless-')}
        outgoing.update({'Authorization':'Bearer '+self.token,
            'Accept-Encoding':'identity',
            'X-Video-Use-Attempt':self.identity.attempt_id,
            'X-Video-Use-Generation':str(self.identity.generation),
            'X-Video-Use-Sandbox':self.identity.sandbox_id})
        return self.session.request('POST',self.endpoint+path,data=body,headers=outgoing,
            stream=True,timeout=(5,90),allow_redirects=False,verify=self.ca_file)

    def close(self):self.session.close()


class GatewayClientServer:
    """Loopback-only HTTP/1.1 bridge with bounded streaming responses."""
    def __init__(self,client,host='127.0.0.1',port=18766):
        if host!='127.0.0.1':raise GatewayClientError('Gateway Client must bind IPv4 loopback')
        outer=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def reply(self,status,payload):
                body=json.dumps(payload,separators=(',',':')).encode()
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
                self.end_headers();self.wfile.write(body);self.close_connection=True
            def do_GET(self):self.reply(405,{'error':'method_not_allowed'})
            def do_POST(self):
                response=None
                try:
                    if self.path!=urlsplit(self.path).path:
                        raise GatewayClientError('Gateway query parameters are not granted')
                    try:size=int(self.headers.get('Content-Length',''))
                    except ValueError:raise GatewayClientError('Invalid Gateway request length') from None
                    if not 0<size<=32*1024*1024:
                        raise GatewayClientError('Invalid Gateway request length')
                    response=outer.client.forward('POST',self.path,dict(self.headers),self.rfile.read(size))
                    if 300<=response.status_code<400:
                        self.reply(502,{'error':'gateway_redirect_rejected'});return
                    self.send_response(response.status_code)
                    content_type=response.headers.get('Content-Type','application/octet-stream')
                    self.send_header('Content-Type',content_type)
                    self.send_header('Connection','close');self.close_connection=True
                    self.end_headers()
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:self.wfile.write(chunk);self.wfile.flush()
                except GatewayClientError as exc:self.reply(403,{'error':str(exc)})
                except (requests.ConnectionError,requests.Timeout):self.reply(502,{'error':'gateway_unavailable'})
                except (BrokenPipeError,ConnectionResetError):pass
                finally:
                    if response is not None:response.close()
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            def __init__(self,*args,**kwargs):
                self.connections=threading.BoundedSemaphore(16);super().__init__(*args,**kwargs)
            def process_request(self,request,client_address):
                if not self.connections.acquire(blocking=False):
                    self.shutdown_request(request);return
                try:super().process_request(request,client_address)
                except Exception:self.connections.release();raise
            def process_request_thread(self,request,client_address):
                try:super().process_request_thread(request,client_address)
                finally:self.connections.release()
        self.client=client;self.server=Server((host,port),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    @property
    def address(self):return self.server.server_address
    def start(self):self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close()
        if self.thread.is_alive():self.thread.join(timeout=3)
        self.client.close()
