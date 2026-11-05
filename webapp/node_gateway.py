"""Host-side capability grants in front of the shared Gateway business core.

The in-memory store is for contract tests and single-process development only.
Production uses the same interface with atomic database reservations.
"""
from __future__ import annotations

from dataclasses import dataclass,field
import hashlib
import ipaddress
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from .runtime_protocol import RuntimeIdentity


class GatewayAuthorizationError(PermissionError):
    pass


class GatewayBudgetError(RuntimeError):
    pass


@dataclass
class Grant:
    id: str
    token_hash: str
    task_id: str
    attempt_id: str
    generation: int
    sandbox_id: str
    source_ip: str
    routes: frozenset[str]
    expires_at: float
    max_calls: int
    max_request_bytes: int
    max_response_bytes: int
    calls: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    revoked: bool = False
    active: dict = field(default_factory=dict,repr=False)


@dataclass(frozen=True)
class IssuedGrant:
    id: str
    token: str = field(repr=False)


def token_hash(token):return hashlib.sha256(token.encode()).hexdigest()


class InMemoryGrantStore:
    def __init__(self,clock=time.time):
        self.clock=clock;self.lock=threading.RLock();self.by_id={};self.by_hash={}

    def add(self,grant):
        with self.lock:
            if grant.id in self.by_id or grant.token_hash in self.by_hash:
                raise ValueError('Duplicate Gateway grant')
            self.by_id[grant.id]=grant;self.by_hash[grant.token_hash]=grant.id

    def reserve(self,digest,identity,path,source_ip,request_bytes):
        with self.lock:
            identifier=self.by_hash.get(digest);grant=self.by_id.get(identifier)
            if not grant or grant.revoked or grant.expires_at<=self.clock():
                raise GatewayAuthorizationError('Gateway grant is unavailable')
            expected=(grant.attempt_id,grant.generation,grant.sandbox_id)
            if identity!=expected or source_ip!=grant.source_ip or path not in grant.routes:
                raise GatewayAuthorizationError('Gateway request is outside its grant')
            if type(request_bytes) is not int or request_bytes<=0:
                raise GatewayBudgetError('Invalid Gateway request size')
            if grant.calls>=grant.max_calls or grant.request_bytes+request_bytes>grant.max_request_bytes:
                raise GatewayBudgetError('Gateway request budget exhausted')
            grant.calls+=1;grant.request_bytes+=request_bytes
            return grant.id

    def consume_response(self,identifier,size):
        with self.lock:
            grant=self.by_id.get(identifier)
            if not grant or grant.revoked or grant.expires_at<=self.clock():
                raise GatewayAuthorizationError('Gateway grant was revoked during the call')
            if type(size) is not int or size<0 or grant.response_bytes+size>grant.max_response_bytes:
                raise GatewayBudgetError('Gateway response budget exhausted')
            grant.response_bytes+=size

    def register(self,identifier,call_id,closer):
        close=False
        with self.lock:
            grant=self.by_id.get(identifier)
            if not grant or grant.revoked or grant.expires_at<=self.clock():close=True
            elif call_id in grant.active:raise ValueError('Duplicate active Gateway call')
            else:grant.active[call_id]=closer
        if close:
            closer();raise GatewayAuthorizationError('Gateway grant is no longer active')

    def unregister(self,identifier,call_id):
        with self.lock:
            grant=self.by_id.get(identifier)
            if grant:grant.active.pop(call_id,None)

    def revoke(self,identifier):
        with self.lock:
            grant=self.by_id.get(identifier)
            if not grant:return False
            grant.revoked=True;closers=list(grant.active.values());grant.active.clear()
        for closer in closers:
            try:closer()
            except Exception:pass
        return True

    def revoke_attempt(self,attempt_id,generation=None):
        with self.lock:
            identifiers=[grant.id for grant in self.by_id.values()
                if grant.attempt_id==attempt_id and (generation is None or grant.generation==generation)]
        for identifier in identifiers:self.revoke(identifier)
        return len(identifiers)


class GrantAuthority:
    def __init__(self,store):self.store=store

    def issue(self,*,task_id,attempt_id,generation,sandbox_id,source_ip,routes,
              expires_at,max_calls,max_request_bytes,max_response_bytes):
        routes=frozenset(routes)
        try:RuntimeIdentity(attempt_id,generation,sandbox_id);ipaddress.ip_address(source_ip)
        except ValueError as exc:raise ValueError('Invalid Gateway grant identity') from exc
        if (not task_id or not routes or expires_at<=self.store.clock() or
                any(type(value) is not int or value<=0 for value in
                    (max_calls,max_request_bytes,max_response_bytes)) or
                any(not isinstance(path,str) or not path.startswith('/') or
                    urlsplit(path).path!=path or '//' in path for path in routes)):
            raise ValueError('Invalid Gateway grant')
        token=secrets.token_urlsafe(32);identifier=uuid.uuid4().hex
        self.store.add(Grant(identifier,token_hash(token),task_id,attempt_id,generation,
            sandbox_id,source_ip,routes,expires_at,max_calls,max_request_bytes,max_response_bytes))
        return IssuedGrant(identifier,token)

    def revoke(self,identifier):return self.store.revoke(identifier)


class CallPermit:
    def __init__(self,store,grant_id):
        self.store=store;self.grant_id=grant_id;self.call_id=uuid.uuid4().hex;self.registered=False

    def register_connection(self,closer):
        self.store.register(self.grant_id,self.call_id,closer);self.registered=True

    def consume_output(self,data):
        size=len(data) if isinstance(data,(bytes,bytearray)) else int(data)
        self.store.consume_response(self.grant_id,size)

    def close(self):
        if self.registered:self.store.unregister(self.grant_id,self.call_id);self.registered=False

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


class NodeGatewayAuthorizer:
    def __init__(self,store):self.store=store

    def authorize(self,headers,path,source_ip,request_bytes):
        headers={str(name).lower():value for name,value in headers.items()}
        authorization=headers.get('authorization','')
        if not authorization.startswith('Bearer ') or not 32<=len(authorization[7:])<=512:
            raise GatewayAuthorizationError('Missing Gateway bearer token')
        token=authorization[7:]
        try:
            identity=(headers['x-video-use-attempt'],int(headers['x-video-use-generation']),
                      headers['x-video-use-sandbox'])
        except (KeyError,ValueError):raise GatewayAuthorizationError('Invalid Gateway identity headers') from None
        grant_id=self.store.reserve(token_hash(token),identity,path,source_ip,request_bytes)
        return CallPermit(self.store,grant_id)


@dataclass
class GatewayResult:
    status: int
    headers: dict
    chunks: object
    close: object


class AuthorizedResult:
    def __init__(self,result,permit):
        self.result=result;self.permit=permit;self.closed=False;self.lock=threading.Lock()
    @property
    def status(self):return self.result.status
    @property
    def headers(self):return self.result.headers
    def __iter__(self):
        try:
            for chunk in self.result.chunks:
                if not isinstance(chunk,(bytes,bytearray)):raise TypeError('Gateway response chunk must be bytes')
                self.permit.consume_output(chunk)
                yield chunk
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
        try:self.result.close()
        finally:self.permit.close()


class NodeGatewayCore:
    """Authorization wrapper around injected, lease-aware business handlers."""
    def __init__(self,authorizer,handlers):self.authorizer=authorizer;self.handlers=dict(handlers)

    def dispatch(self,headers,path,source_ip,body):
        permit=self.authorizer.authorize(headers,path,source_ip,len(body))
        try:
            handler=self.handlers.get(path)
            if handler is None:raise GatewayAuthorizationError('Gateway route has no business handler')
            result=handler(body,permit)
            if not isinstance(result,GatewayResult):raise TypeError('Invalid Gateway handler result')
            authorized=AuthorizedResult(result,permit)
            if not permit.registered:permit.register_connection(authorized.close)
            return authorized
        except Exception:
            permit.close();raise


class RuntimeGrantFactory:
    """Issue a sandbox-bound grant only after OpenSandbox returns its identity."""
    def __init__(self,authority,*,task_id,source_ip,endpoint,ca,routes,ttl=300,
                 max_calls=200,max_request_bytes=64*1024**2,max_response_bytes=256*1024**2):
        self.authority=authority;self.task_id=task_id;self.source_ip=source_ip
        self.endpoint=endpoint;self.ca=ca;self.routes=tuple(routes);self.ttl=ttl
        self.limits=(max_calls,max_request_bytes,max_response_bytes)

    def __call__(self,identity):
        source=self.source_ip(identity) if callable(self.source_ip) else self.source_ip
        issued=self.authority.issue(task_id=self.task_id,attempt_id=identity.attempt_id,
            generation=identity.generation,sandbox_id=identity.sandbox_id,source_ip=source,
            routes=self.routes,expires_at=self.authority.store.clock()+self.ttl,
            max_calls=self.limits[0],max_request_bytes=self.limits[1],
            max_response_bytes=self.limits[2])
        from .opensandbox_backend import RuntimeGatewayGrant
        from .runtime_injection import GatewayGrant
        material=GatewayGrant(self.endpoint,issued.token,self.ca,self.routes)
        return RuntimeGatewayGrant(material,lambda:self.authority.revoke(issued.id))
