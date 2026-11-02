"""Bounded SSE inspection; safe complete events never wait for the next chunk."""
import json
import time
from .egress_policy import assert_no_credentials


class PrefixGuard:
    def __init__(self, protected):
        self.protected=tuple(v for v in protected if v and len(v)>=12)
        self.pending=''
    def feed(self,value):
        value=self.pending+value
        assert_no_credentials(value,self.protected)
        keep=0
        for secret in self.protected:
            for size in range(min(len(secret)-1,len(value)),keep,-1):
                if value.endswith(secret[:size]):keep=size;break
        self.pending=value[-keep:] if keep else ''
        return value[:-keep] if keep else value
    def finish(self):
        value=self.pending;self.pending='';return value


def chunks(response):
    """read1 returns available bytes instead of filling a fixed sized read."""
    raw=getattr(response,'raw',None)
    if raw is not None and callable(getattr(raw,'read1',None)):
        while True:
            value=raw.read1(4096,decode_content=True)
            if not value:return
            yield value
    else:
        yield from response.iter_content(chunk_size=4096)


class SafeEvents:
    limit=32*1024**2
    def __init__(self,protected):
        self.protected=protected;self.buffer=b'';self.pending=[];self.guards={};self.size=0;self.held=0;self.received=None;self.event_sequence=0;self.last_log=0.
    def inspect(self,value):
        if isinstance(value,str):assert_no_credentials(value,self.protected)
        elif isinstance(value,dict):
            for key,item in value.items():self.inspect(key);self.inspect(item)
        elif isinstance(value,list):
            for item in value:self.inspect(item)
    def protect_fragment(self,key,text):
        if not isinstance(text,str):return
        if key not in self.guards:
            if len(self.guards)>=2000:raise ValueError('Too many response channels')
            self.guards[key]=PrefixGuard(self.protected)
        self.guards[key].feed(text)
    def protect_native(self,value):
        """Inspect logical text/tool channels across native SSE event boundaries."""
        kind=value.get('type','');delta=value.get('delta')
        if kind=='content_block_delta' and isinstance(delta,dict):
            for field in ('text','thinking','partial_json'):
                if field in delta:self.protect_fragment(('anthropic',value.get('index'),field),delta[field])
        choices=value.get('choices')
        if isinstance(choices,list):
            for choice in choices:
                if not isinstance(choice,dict):continue
                index=choice.get('index',0);delta=choice.get('delta') or {}
                if isinstance(delta,dict):
                    for field in ('content','reasoning_content'):
                        if field in delta:self.protect_fragment(('chat',index,field),delta[field])
                    for tool in delta.get('tool_calls') or []:
                        if not isinstance(tool,dict) or not isinstance(tool.get('function'),dict):continue
                        for field in ('name','arguments'):
                            self.protect_fragment(('chat',index,tool.get('index'),field),tool['function'].get(field))
                if choice.get('finish_reason'):
                    self.guards={key:guard for key,guard in self.guards.items() if key[:2]!=('chat',index)}
        if kind=='message_stop':
            self.guards={key:guard for key,guard in self.guards.items() if key[0]!='anthropic'}
    def feed(self,chunk):
        self.buffer+=chunk;self.size+=len(chunk)
        if self.size>self.limit:raise ValueError('Model response exceeds output budget')
        while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
            at,sep=min((self.buffer.find(s),s) for s in (b'\n\n',b'\r\n\r\n') if s in self.buffer)
            frame=self.buffer[:at+len(sep)];self.buffer=self.buffer[at+len(sep):]
            assert_no_credentials(frame,self.protected)
            data=b'\n'.join(l[5:].lstrip(b' ') for l in frame.splitlines() if l.startswith(b'data:'))
            kind='';message_id=None;self.event_sequence+=1
            if data and data!=b'[DONE]':
                value=json.loads(data);self.inspect(value)
                if isinstance(value,dict):
                    kind=value.get('type','');message_id=value.get('item_id')
                    if isinstance(value.get('delta'),str):
                        key=(kind,value.get('item_id'),value.get('output_index'),value.get('content_index'))
                        self.protect_fragment(key,value['delta'])
                    self.protect_native(value)
                    # A completed response is a verified logical boundary, not
                    # an arbitrary network chunk or SSE event boundary.
                    if kind in ('response.completed','response.failed','response.incomplete','response.cancelled'):self.guards.clear()
            self.pending.append(frame);self.held+=len(frame)
            if self.received is None:self.received=time.monotonic()
            if not any(g.pending for g in self.guards.values()):
                from .telemetry import emit
                if time.monotonic()-self.last_log>=1 or kind.endswith(('.done','.completed','.failed')):
                    emit('stream.gateway_forward',phase=kind or 'event',message_id=message_id,event_sequence=self.event_sequence,duration_seconds=time.monotonic()-self.received,bytes_sent=self.held)
                    self.last_log=time.monotonic()
                yield b''.join(self.pending);self.pending=[];self.held=0;self.received=None
    def finish(self):
        if self.pending:raise ValueError('Model stream ended with unresolved protected prefix')
        if self.buffer.strip():
            # Non-streaming JSON responses are checked as a complete document.
            value=json.loads(self.buffer);self.inspect(value);assert_no_credentials(self.buffer,self.protected)
            yield self.buffer
        self.buffer=b''
