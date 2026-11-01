"""Only fixed provider destinations and inline media are allowed at the broker."""
import base64
import ipaddress
import re
import os
import socket
import threading
import queue
import time
from urllib.parse import urlsplit

_dns_slots=threading.BoundedSemaphore(4)


def resolve(host,port,timeout):
    # libc DNS resolution has no per-call timeout. Isolate it in a bounded
    # daemon pool so a stuck resolver cannot indefinitely block task cleanup.
    if not _dns_slots.acquire(blocking=False):raise OSError('Provider resolver is busy')
    result=queue.Queue(maxsize=1)
    def run():
        try:result.put((socket.getaddrinfo(host,port,type=socket.SOCK_STREAM),None))
        except Exception as error:result.put((None,error))
        finally:_dns_slots.release()
    threading.Thread(target=run,daemon=True).start()
    try:records,error=result.get(timeout=min(5,timeout))
    except queue.Empty:raise OSError('Provider DNS lookup timed out') from None
    if error:raise error
    return records


def session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection,HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool,HTTPSConnectionPool
    allowed={name.strip().lower() for name in os.environ.get('VIDEO_USE_INTERNAL_PROVIDER_HOSTS','localhost,127.0.0.1,::1').split(',') if name.strip()}
    lifetime=float(os.environ.get('VIDEO_USE_PROVIDER_REQUEST_SECONDS','240'))
    from .task_leases import CURRENT
    lease=CURRENT.get()
    if lease:
        from .task_lifecycle import snapshot
        state=snapshot(lease.store,lease.turn_id)
        lifetime=min(lifetime,max(.01,state['deadline_at']-lease.store.now()))
        if lease.runtime:lifetime=min(lifetime,max(.01,state['remaining_seconds']))
    class PinnedConnection:
        def close(self):
            self._deadline_cancelled=True
            monitored=getattr(self,'_cancel_call',None)
            if monitored:monitored.close();self._cancel_call=None
            guard=getattr(self,'_cancel_socket',None)
            if guard:guard.close();self._cancel_socket=None
            timer=getattr(self,'_deadline_timer',None)
            if timer:timer.cancel()
            super().close()
        def _new_conn(self):
            records=resolve(self._dns_host,self.port,lifetime)
            for _,_,_,_,address in records:
                ip=ipaddress.ip_address(address[0]);ip=getattr(ip,'ipv4_mapped',None) or ip
                if ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) in {'100.100.100.200','fd00:ec2::254'}:
                    raise OSError('Forbidden provider network destination')
                if not ip.is_global and self._dns_host.lower() not in allowed:
                    raise OSError('Internal provider host is not explicitly allowed')
            last=None
            for family,kind,protocol,_,address in records:
                sock=socket.socket(family,kind,protocol)
                monitored=None
                guard=None
                try:
                    if lease:
                        from .cancellation import for_store,interrupt_socket
                        monitored=for_store(lease.store).register_lease(lease,'http')
                        self._cancel_call=monitored
                        guard=sock.dup();self._cancel_socket=guard
                        with monitored.lock:
                            monitored.hard_deadline=time.monotonic()+lifetime
                            monitored.deadline=min(monitored.deadline,monitored.hard_deadline)
                        def interrupt(owned=guard):
                            # This duplicate owns the same kernel socket even
                            # while TLS wraps/detaches the original descriptor.
                            interrupt_socket(owned)
                        monitored.install(interrupt)
                        monitored.check()
                    sock.settimeout(min(self.timeout,lifetime) if isinstance(self.timeout,(int,float)) else lifetime)
                    for option in self.socket_options or []:sock.setsockopt(*option)
                    if self.source_address:sock.bind(self.source_address)
                    sock.connect(address)  # Connect to the validated address, no second DNS lookup.
                    if monitored:monitored.check()
                    if sock.getpeername()[0]!=address[0]:raise OSError('Provider peer address changed')
                    def expire():
                        current=getattr(self,'sock',None)
                        if current:
                            try:current.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
                            current.close()
                    self._deadline_cancelled=False
                    if not lease:
                        self._deadline_timer=threading.Timer(lifetime,expire)
                        self._deadline_timer.daemon=True;self._deadline_timer.start()
                    return sock
                except OSError as exc:
                    last=exc;sock.close()
                    if guard:guard.close()
                    if monitored:monitored.close()
                except BaseException:
                    sock.close()
                    if guard:guard.close()
                    if monitored:monitored.close()
                    raise
            raise last or OSError('Provider has no allowed address')
    class HTTP(PinnedConnection,HTTPConnection):pass
    class HTTPS(PinnedConnection,HTTPSConnection):pass
    class HTTPPool(HTTPConnectionPool):ConnectionCls=HTTP
    class HTTPSPool(HTTPSConnectionPool):ConnectionCls=HTTPS
    adapter=HTTPAdapter()
    adapter.poolmanager.pool_classes_by_scheme={'http':HTTPPool,'https':HTTPSPool}
    class BoundedSession(requests.Session):
        def send(self,request,**kwargs):
            from .telemetry import span
            with span('provider_http_headers' if kwargs.get('stream',False) else 'provider_http'):
                return self.observed_send(request,**kwargs)
        def observed_send(self,request,**kwargs):
            streaming=kwargs.get('stream',False);kwargs['stream']=True
            response=super().send(request,**kwargs)
            from .telemetry import provider_response
            provider_response(response)
            if not streaming:
                data=bytearray()
                try:
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data)>32*1024**2:raise ValueError('Provider response exceeds size limit')
                    response._content=bytes(data);response._content_consumed=True
                except Exception:response.close();raise
            return response
    value=BoundedSession();value.trust_env=False
    value.mount('http://',adapter);value.mount('https://',adapter)
    return value


def secrets():
    return tuple(value for key,value in os.environ.items() if len(value)>=12 and
                 (key.upper().endswith(('_KEY','_TOKEN','_PASSWORD')) or 'SECRET' in key.upper()))


def assert_no_credentials(raw,extra=()):
    if isinstance(raw,str):raw=raw.encode()
    if any(value.encode() in raw for value in (*secrets(),*extra) if value and len(value)>=12):
        raise ValueError('Detected a private credential; content was not published')


def copy_public(source,target,digest=None):
    protected=secrets();overlap=max([len(value.encode()) for value in protected]+[1])-1
    previous=b''
    while chunk:=source.read(1024*1024):
        assert_no_credentials(previous+chunk,protected)
        previous=(previous+chunk)[-overlap:] if overlap else b''
        target.write(chunk)
        if digest is not None:digest.update(chunk)


def denial(lease,category):
    if not lease:return
    with lease.bind(),lease.store.connect() as db:
        db.execute('INSERT INTO execution_security_events(task_id,category,created) VALUES (?,?,?)',(lease.turn_id,category,lease.store.now()))
        count=db.execute('SELECT COUNT(*) AS n FROM execution_security_events WHERE task_id=?',(lease.turn_id,)).fetchone()['n']
        if count>=int(os.environ.get('VIDEO_USE_SECURITY_DENIAL_LIMIT','8')):
            from .task_lifecycle import terminate
            terminate(db,lease.turn_id,'security')


def provider_base(value):
    parsed=urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Invalid configured provider address')
    try:address=ipaddress.ip_address(parsed.hostname)
    except ValueError:address=None
    if address and (address.is_link_local or address.is_multicast or address.is_unspecified or str(address) in {'100.100.100.200','fd00:ec2::254'}):
        raise ValueError('Metadata/link-local provider destinations are forbidden')
    if parsed.hostname.lower() in ('metadata.google.internal','metadata.google','instance-data'):
        raise ValueError('Metadata provider destinations are forbidden')
    return value.rstrip('/')


def model_input(body):
    """A user URL must never turn into an upstream fetch capability.

    Text mentioning URLs remains text. Active image/file/tool payloads are
    restricted; this is not a keyword blacklist over user conversation.
    """
    if not isinstance(body,dict):raise ValueError('Invalid model request')
    nodes=[(body,0)]
    count=0
    while nodes:
        value,depth=nodes.pop();count+=1
        if depth>40 or count>100000:raise ValueError('Model input is too complex')
        if isinstance(value,list):nodes.extend((item,depth+1) for item in value)
        elif isinstance(value,dict):
            if '$ref' in value and (not isinstance(value['$ref'],str) or not value['$ref'].startswith('#')):
                raise ValueError('External schema references are not enabled')
            kind=value.get('type')
            if kind=='input_file':raise ValueError('Remote/file-reference model inputs are not enabled')
            if kind in ('input_image','image_url'):
                image=value.get('image_url',value.get('url'))
                if isinstance(image,dict):image=image.get('url')
                if not isinstance(image,str) or not re.match(r'^data:image/(png|jpeg|webp|gif);base64,',image):
                    raise ValueError('Images must use authorized inline bytes, not URLs or file IDs')
                encoded=image.split(',',1)[1]
                if len(encoded)>28*1024**2:raise ValueError('Image input is too large')
                try:raw=base64.b64decode(encoded,validate=True)
                except ValueError:raise ValueError('Invalid image encoding') from None
                if not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff') or raw.startswith((b'GIF87a',b'GIF89a')) or raw[:4]==b'RIFF' and raw[8:12]==b'WEBP'):
                    raise ValueError('Unsupported inline image format')
            if kind in ('input_video','input_audio'):raise ValueError('Active remote media inputs are not enabled')
            nodes.extend((item,depth+1) for item in value.values())
    body.pop('conversation',None)
    body.pop('previous_response_id',None)
    body.pop('background',None)
    body.pop('service_tier',None)
    allowed={'model','input','instructions','tools','tool_choice','parallel_tool_calls','max_output_tokens',
             'reasoning','text','stream','store','include','temperature','top_p','truncation','metadata',
             'prompt_cache_key','prompt_cache_retention','stream_options'}
    for key in list(body):
        if key not in allowed:body.pop(key)
    if 'tools' in body:
        body['tools']=[tool for tool in body['tools'] if isinstance(tool,dict) and tool.get('type') in ('function','custom','namespace')]
    return body


def scrub(text,secrets=(),paths=()):
    for secret in secrets:
        if secret and len(secret)>=8:text=text.replace(secret,'[redacted]')
    for source,target in paths:
        if source:text=text.replace(str(source),target)
    return text
