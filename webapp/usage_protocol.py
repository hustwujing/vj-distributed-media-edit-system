"""Bounded, content-discarding observer for Responses JSON and SSE receipts."""
import json
import re

FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens')


def number(value):
    return value if type(value) is int and 0 <= value <= 2**53-1 else None


def normalize(value):
    if not isinstance(value, dict):return {key:None for key in FIELDS}
    result={key:number(value.get(key)) for key in FIELDS[:3]}
    for field,container in [('cached_tokens','input_tokens_details'),('reasoning_tokens','output_tokens_details')]:
        details=value.get(container)
        result[field]=number(details.get(field)) if isinstance(details,dict) else None
    return result


class Receipt:
    """Never changes forwarded bytes. Missing final receipts remain unknown."""
    limit=32*1024**2
    def __init__(self):
        self.buffer=b'';self.sse=None;self.usage=None;self.status=None;self.response_id='';self.truncated=False

    def accept(self, data):
        try:value=json.loads(data)
        except (ValueError,UnicodeError):return
        if not isinstance(value,dict):return
        kind=value.get('type','')
        callback=getattr(self,'on_activity',None)
        if callback:
            item=value.get('item') if isinstance(value.get('item'),dict) else {}
            if kind=='response.output_text.delta':callback('responding')
            elif kind.startswith('response.reasoning') or kind=='response.output_item.added' and item.get('type')=='reasoning':callback('analysis')
            elif kind=='response.output_item.added' and item.get('type') in ('function_call','custom_tool_call'):
                self._tool_phase='writing' if item.get('name') in ('apply_patch','functions.apply_patch') else 'prepare_tool'
                callback(self._tool_phase)
            elif kind in ('response.function_call_arguments.delta','response.custom_tool_call_input.delta'):callback(getattr(self,'_tool_phase','prepare_tool'))
        response=value.get('response') if isinstance(value.get('response'),dict) else value
        status=response.get('status')
        if kind in ('response.completed','response.failed','response.incomplete','response.cancelled') or status in ('completed','failed','incomplete','cancelled'):
            self.status={'completed':'succeeded','failed':'failed','incomplete':'incomplete','cancelled':'cancelled'}.get(status or kind.split('.')[-1],'unknown')
            identifier=response.get('id','')
            if isinstance(identifier,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}',identifier):self.response_id=identifier
            if isinstance(response.get('usage'),dict):self.usage=normalize(response['usage'])

    def feed(self,chunk):
        if self.truncated:return
        self.buffer+=chunk
        if len(self.buffer)>self.limit:
            self.buffer=b'';self.truncated=True;return
        if self.sse is None:
            prefix=self.buffer.lstrip()
            if prefix.startswith(b'{'):self.sse=False
            elif b'\n' in prefix or prefix.startswith((b'data:',b'event:',b':')):self.sse=True
        if self.sse:
            # Split lines, not chunks; a CRLF may be split across network reads.
            while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
                positions=[(self.buffer.find(sep),sep) for sep in (b'\n\n',b'\r\n\r\n') if sep in self.buffer]
                at,sep=min(positions,key=lambda x:x[0]);frame=self.buffer[:at];self.buffer=self.buffer[at+len(sep):]
                data=b'\n'.join(line[5:].lstrip(b' ') for line in frame.splitlines() if line.startswith(b'data:'))
                if data:self.accept(data)

    def finish(self):
        if self.buffer and not self.truncated:
            if self.sse:
                data=b'\n'.join(line[5:].lstrip(b' ') for line in self.buffer.splitlines() if line.startswith(b'data:'))
                if data:self.accept(data)
            else:self.accept(self.buffer)
        self.buffer=b''
