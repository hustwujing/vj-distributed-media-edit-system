"""Bounded bidirectional protocol between an Executor and Runtime Daemon.

This module is transport independent.  A WebSocket adapter only moves JSON
frames; identity fencing, request idempotency, event ordering and replay live
here so they can be tested without a sandbox or network service.
"""
from __future__ import annotations

from collections import OrderedDict,deque
from dataclasses import dataclass
import hmac
import json
import re
import threading

from .harness_events import AgentEvent


PROTOCOL_VERSION=1
MAX_FRAME_BYTES=1024*1024


class ProtocolError(ValueError):
    pass


class AuthenticationError(ProtocolError):
    pass


class BackpressureExceeded(RuntimeError):
    pass


class ReplayUnavailable(ProtocolError):
    pass


@dataclass(frozen=True)
class RuntimeIdentity:
    attempt_id: str
    generation: int
    sandbox_id: str

    def __post_init__(self):
        valid=lambda value:bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}',value or ''))
        if not valid(self.attempt_id) or not valid(self.sandbox_id) or self.generation<1:
            raise ProtocolError('Invalid runtime identity')

    def envelope(self):
        return {'protocol':PROTOCOL_VERSION,'attempt_id':self.attempt_id,
                'generation':self.generation,'sandbox_id':self.sandbox_id}


def encode_frame(frame):
    data=json.dumps(frame,ensure_ascii=False,separators=(',',':')).encode()
    if len(data)>MAX_FRAME_BYTES:raise ProtocolError('Runtime protocol frame exceeds limit')
    return data


def decode_frame(data):
    if isinstance(data,str):data=data.encode()
    if not isinstance(data,(bytes,bytearray)) or len(data)>MAX_FRAME_BYTES:
        raise ProtocolError('Invalid runtime protocol frame')
    try:value=json.loads(data)
    except (UnicodeDecodeError,ValueError) as exc:raise ProtocolError('Malformed runtime protocol JSON') from exc
    if not isinstance(value,dict):raise ProtocolError('Runtime protocol frame must be an object')
    return value


class EventBuffer:
    """Retain every unacknowledged event; never evict correctness evidence."""
    def __init__(self,identity,*,max_events=256,max_bytes=4*1024*1024):
        self.identity=identity;self.max_events=max_events;self.max_bytes=max_bytes
        self.next_seq=1;self.acked_through=0;self.bytes=0;self.frames=deque()
        self.lock=threading.RLock()

    def publish(self,event):
        if not isinstance(event,AgentEvent):raise ProtocolError('Invalid runtime event')
        with self.lock:
            frame={**self.identity.envelope(),'type':'event','seq':self.next_seq,
                   'event':{'kind':event.kind,'id':event.id,'data':event.data}}
            size=len(encode_frame(frame))
            if len(self.frames)>=self.max_events or self.bytes+size>self.max_bytes:
                raise BackpressureExceeded('Runtime event ACK window is full')
            self.frames.append((self.next_seq,size,frame));self.bytes+=size;self.next_seq+=1
            return frame

    def acknowledge(self,sequence):
        with self.lock:
            if not isinstance(sequence,int) or sequence<self.acked_through or sequence>=self.next_seq:
                raise ProtocolError('Invalid runtime event ACK')
            self.acked_through=sequence
            while self.frames and self.frames[0][0]<=sequence:
                _,size,_=self.frames.popleft();self.bytes-=size

    def replay(self,after_sequence):
        with self.lock:
            if not isinstance(after_sequence,int) or after_sequence<0 or after_sequence>=self.next_seq:
                raise ProtocolError('Invalid runtime replay cursor')
            if after_sequence<self.acked_through:
                raise ReplayUnavailable('Requested runtime events were already acknowledged')
            return [frame for sequence,_,frame in self.frames if sequence>after_sequence]

    def status(self):
        with self.lock:return self.acked_through,self.next_seq


class DaemonProtocol:
    """Authenticated server-side state for one Runtime Daemon process."""
    METHODS={'runtime.initialize','session.open','turn.start','turn.interrupt','session.export','session.freeze'}

    def __init__(self,identity,token,*,max_events=256,max_bytes=4*1024*1024,max_responses=128):
        if not token:raise AuthenticationError('Runtime control token is required')
        self.identity=identity;self._token=token;self.authenticated=False
        self.events=EventBuffer(identity,max_events=max_events,max_bytes=max_bytes)
        self.responses=OrderedDict();self.max_responses=max_responses

    def _validate_identity(self,frame):
        expected=self.identity.envelope()
        if any(frame.get(key)!=value for key,value in expected.items()):
            raise AuthenticationError('Runtime frame identity does not match sandbox')

    def disconnect(self):
        self.authenticated=False

    def publish(self,event):
        return self.events.publish(event)

    def receive(self,frame,dispatch):
        encode_frame(frame);self._validate_identity(frame)
        kind=frame.get('type')
        if kind=='hello':
            if self.authenticated:raise ProtocolError('Runtime connection is already authenticated')
            if not hmac.compare_digest(str(frame.get('token','')),self._token):
                raise AuthenticationError('Invalid runtime control token')
            replay=self.events.replay(frame.get('after_seq',0));self.authenticated=True
            acked,next_sequence=self.events.status()
            return [{**self.identity.envelope(),'type':'hello.ack',
                     'acked_through':acked,'next_seq':next_sequence},*replay]
        if not self.authenticated:raise AuthenticationError('Runtime handshake is required')
        if kind=='ack':
            self.events.acknowledge(frame.get('seq'));return []
        if kind!='request':raise ProtocolError('Unsupported runtime frame type')
        request_id=frame.get('id');method=frame.get('method');params=frame.get('params',{})
        if not isinstance(request_id,str) or not request_id or len(request_id)>128:
            raise ProtocolError('Invalid runtime request id')
        if method not in self.METHODS or not isinstance(params,dict):
            raise ProtocolError('Unsupported runtime request')
        fingerprint=encode_frame({'method':method,'params':params})
        previous=self.responses.get(request_id)
        if previous:
            if previous[0]!=fingerprint:raise ProtocolError('Runtime request id was reused')
            self.responses.move_to_end(request_id);return [previous[1]]
        try:
            response={**self.identity.envelope(),'type':'response','id':request_id,
                      'result':dispatch(method,params)}
        except Exception as exc:
            response={**self.identity.envelope(),'type':'response','id':request_id,
                      'error':{'code':'runtime_request_failed',
                               'message':'Runtime request failed ('+type(exc).__name__+')'}}
        encode_frame(response);self.responses[request_id]=(fingerprint,response)
        while len(self.responses)>self.max_responses:self.responses.popitem(last=False)
        return [response]


class ExecutorProtocol:
    """Client-side ordering and correlation state used by the Executor."""
    def __init__(self,identity,token):
        self.identity=identity;self.token=token;self.last_seq=0;self.next_request=1
        self.connected=False;self.responses={};self.pending=set()

    def hello(self):
        return {**self.identity.envelope(),'type':'hello','token':self.token,'after_seq':self.last_seq}

    def request(self,method,params):
        request_id=str(self.next_request);self.next_request+=1
        self.pending.add(request_id)
        return {**self.identity.envelope(),'type':'request','id':request_id,
                'method':method,'params':params}

    def acknowledge(self,sequence=None):
        return {**self.identity.envelope(),'type':'ack','seq':self.last_seq if sequence is None else sequence}

    def receive(self,frame):
        encode_frame(frame)
        expected=self.identity.envelope()
        if any(frame.get(key)!=value for key,value in expected.items()):
            raise AuthenticationError('Foreign runtime frame')
        kind=frame.get('type')
        if kind=='hello.ack':self.connected=True;return None
        if not self.connected:raise AuthenticationError('Runtime hello acknowledgement is required')
        if kind=='response':
            request_id=frame.get('id')
            if not isinstance(request_id,str) or request_id not in self.pending:
                raise ProtocolError('Unexpected runtime response')
            self.pending.remove(request_id)
            self.responses[request_id]=frame;return None
        if kind!='event':raise ProtocolError('Unsupported daemon frame type')
        sequence=frame.get('seq')
        if not isinstance(sequence,int):raise ProtocolError('Invalid runtime event sequence')
        if sequence<=self.last_seq:return None
        if sequence!=self.last_seq+1:raise ProtocolError('Runtime event sequence has a gap')
        raw=frame.get('event',{})
        try:event=AgentEvent(raw['kind'],raw.get('id',''),raw.get('data',{}))
        except (KeyError,TypeError,ValueError) as exc:raise ProtocolError('Invalid runtime event payload') from exc
        self.last_seq=sequence
        return event
