"""Native model wire protocols behind the same fenced, metered gateway.

Adapters neither own credentials nor retry requests. The gateway owns each
network attempt, cancellation, and the authoritative usage receipt.
"""
import base64
import copy
import json
from .usage_protocol import Receipt, number


class PublicResponseStructureError(ValueError):
    def __init__(self,message,code='multiple_public_blocks'):
        self.code=code
        super().__init__(message)


class StructuredStream:
    """Rebuild one completed native tool response while releasing only public text."""
    limit=8*1024**2
    def __init__(self,protocol,on_text):
        self.protocol,self.on_text=protocol,on_text;self.buffer=b'';self.size=0;self.payload=None;self.sse=False
        self.message={};self.blocks={};self.chat={};self.usage=None
    def feed(self,chunk):
        self.size+=len(chunk)
        if self.size>self.limit:raise ValueError('Model response exceeds limit')
        self.buffer+=chunk
        while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
            at,sep=min((self.buffer.find(value),value) for value in (b'\n\n',b'\r\n\r\n') if value in self.buffer)
            frame,self.buffer=self.buffer[:at],self.buffer[at+len(sep):]
            data=b'\n'.join(line[5:].lstrip(b' ') for line in frame.splitlines() if line.startswith(b'data:'))
            if data:self.sse=True
            if data and data!=b'[DONE]':self.accept(json.loads(data))
    def accept(self,value):
        if not isinstance(value,dict):return
        if self.protocol=='responses':
            if value.get('type')=='response.output_text.delta' and isinstance(value.get('delta'),str):self.on_text(value['delta'])
            if value.get('type')=='response.completed' and isinstance(value.get('response'),dict):self.payload=value['response']
            return
        if self.protocol=='anthropic':
            kind=value.get('type');index=value.get('index',0)
            if kind=='message_start':self.message=dict(value.get('message') or {});self.message['content']=[]
            elif kind=='content_block_start':
                block=dict(value.get('content_block') or {});block['_json']='';self.blocks[index]=block
                if block.get('type')=='text' and isinstance(block.get('text'),str) and block['text']:self.on_text(block['text'])
            elif kind=='content_block_delta':
                delta=value.get('delta') or {};block=self.blocks.setdefault(index,{'type':'text','text':'','_json':''})
                if delta.get('type')=='text_delta' and isinstance(delta.get('text'),str):block['text']=block.get('text','')+delta['text'];self.on_text(delta['text'])
                elif delta.get('type')=='input_json_delta' and isinstance(delta.get('partial_json'),str):block['_json']+=delta['partial_json']
            elif kind=='content_block_stop':
                block=self.blocks.get(index,{})
                if block.get('type')=='tool_use':block['input']=json.loads(block.pop('_json','') or '{}')
            elif kind=='message_delta':self.message.update(value.get('delta') or {});self.message['usage']={**(self.message.get('usage') or {}),**(value.get('usage') or {})}
            elif kind=='message_stop':
                self.message['content']=[{k:v for k,v in self.blocks[key].items() if k!='_json'} for key in sorted(self.blocks)];self.payload=self.message
            return
        if self.protocol=='chat_completions':
            if isinstance(value.get('usage'),dict):self.usage=value['usage']
            for choice in value.get('choices',[]):
                index=choice.get('index',0);row=self.chat.setdefault(index,{'content':'','tool_calls':{},'finish_reason':None});delta=choice.get('delta') or {}
                if isinstance(delta.get('content'),str):row['content']+=delta['content'];self.on_text(delta['content'])
                for tool in delta.get('tool_calls') or []:
                    tool_index=tool.get('index',0);target=row['tool_calls'].setdefault(tool_index,{'id':'','type':'function','function':{'name':'','arguments':''}});target['id']+=tool.get('id','');target['type']=tool.get('type',target['type']);function=tool.get('function') or {};target['function']['name']+=function.get('name','');target['function']['arguments']+=function.get('arguments','')
                if choice.get('finish_reason') is not None:row['finish_reason']=choice['finish_reason']
    def finish(self):
        if self.buffer.strip():
            prefix=self.buffer.lstrip()
            if prefix.startswith(b'{'):self.payload=json.loads(self.buffer)
            else:
                data=b'\n'.join(line[5:].lstrip(b' ') for line in self.buffer.splitlines() if line.startswith(b'data:'))
                if data and data!=b'[DONE]':self.accept(json.loads(data))
        self.buffer=b''
        if self.protocol=='chat_completions' and self.payload is None and self.chat:
            self.payload={'choices':[{'index':index,'finish_reason':row['finish_reason'],'message':{'role':'assistant','content':row['content'] or None,'tool_calls':[row['tool_calls'][key] for key in sorted(row['tool_calls'])]}} for index,row in sorted(self.chat.items())]}
            if self.usage is not None:self.payload['usage']=self.usage
        if not isinstance(self.payload,dict):raise ValueError('Model stream ended without a complete response')
        return self.payload


class ModelAdapter:
    protocol = ''
    path = ''

    def headers(self, key):
        return {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                'Accept': 'text/event-stream', 'User-Agent': 'video-use-agent'}

    def prepare(self, body, model, max_output_tokens, reasoning_effort=None):
        raise NotImplementedError

    def receipt(self):
        return Receipt()

    def conversation(self,instructions,text,images=()):
        raise NotImplementedError('Structured conversations are not implemented for this protocol')

    def response_text(self,payload):
        raise NotImplementedError('Structured response parsing is not implemented for this protocol')

    def public_commentary(self,payload):
        return []

    def input_token_request(self,body):
        raise NotImplementedError('This protocol has no configured multimodal input token counter')


class ResponsesAdapter(ModelAdapter):
    protocol = 'responses'
    path = '/responses'

    def input_token_request(self,body):
        return '/responses/input_tokens',{k:body[k] for k in ('model','instructions','input','tools','tool_choice','text') if k in body}

    def conversation(self,instructions,text,images=()):
        content=[{'type':'input_text','text':text}]+[{'type':'input_image','image_url':image} for image in images]
        return {'instructions':instructions,'input':[{'role':'user','content':content}],'stream':False}

    def response_text(self,payload):
        if not isinstance(payload,dict):raise ValueError('Response must be an object')
        if payload.get('status')!='completed':raise ValueError('Response is not complete')
        output=payload.get('output',[])
        if not isinstance(output,list) or any(not isinstance(item,dict) for item in output):raise ValueError('Response output must be typed items')
        messages=[item for item in output if item.get('type')=='message']
        if any(item.get('role','assistant')!='assistant' or item.get('status') not in (None,'completed') for item in messages):
            raise ValueError('Response contains an incomplete or non-assistant message')
        if any(item.get('phase') not in (None,'commentary','final_answer') for item in messages):
            raise PublicResponseStructureError('Unsupported public message phase','unsupported_response_phase')
        # Commentary is a progress message, never an executable Agent action.
        # Exactly one final message is required; never concatenate or choose the
        # last of several competing final answers.
        final=[item for item in messages if item.get('phase')=='final_answer']
        unphased=[item for item in messages if item.get('phase') is None]
        if final and unphased:raise PublicResponseStructureError('Structured Agent requires exactly one unambiguous final message')
        messages=final or unphased
        if not messages:raise PublicResponseStructureError('Response has no final public message','missing_final_message')
        texts=[c.get('text','') for item in messages for c in item.get('content',[]) if c.get('type')=='output_text']
        if len(messages)!=1 or len(texts)!=1 or not isinstance(texts[0],str):
            raise PublicResponseStructureError('Structured Agent requires exactly one public text block')
        return texts[0]

    def public_commentary(self,payload):
        output=payload.get('output',[]) if isinstance(payload,dict) else []
        return [content['text'] for item in output if isinstance(item,dict) and item.get('type')=='message' and item.get('phase')=='commentary'
                for content in item.get('content',[]) if isinstance(content,dict) and content.get('type')=='output_text' and isinstance(content.get('text'),str)]

    def prepare(self, body, model, max_output_tokens, reasoning_effort=None):
        from .egress_policy import model_input
        value = model_input(copy.deepcopy(body))
        value['model'] = model
        value['store'] = False
        value['max_output_tokens'] = bounded_output(value.get('max_output_tokens'), max_output_tokens)
        if reasoning_effort:
            value['reasoning']={**(value.get('reasoning') or {}),'effort':reasoning_effort}
        return value


def bounded_output(value, cap):
    if value is None:
        return cap
    if type(value) is not int or value < 1:
        raise ValueError('Invalid model output limit')
    return min(value, cap)


def validate_messages(messages):
    from .egress_policy import model_input
    if not isinstance(messages, list) or len(messages) > 20000:
        raise ValueError('Invalid model conversation')
    # Reuse the active URL/file/schema validation, without applying the
    # Responses top-level field whitelist to a different wire protocol.
    model_input({'input': messages})
    stack = [(messages, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 40:
            raise ValueError('Model content nesting exceeds limit')
        if isinstance(value, list):
            stack.extend((x, depth + 1) for x in value)
        elif isinstance(value, dict):
            kind = value.get('type')
            if kind in ('document', 'file', 'url', 'server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'):
                raise ValueError('Remote model content is not enabled')
            if kind == 'image':
                source = value.get('source') or {}
                if not isinstance(source, dict) or source.get('type') != 'base64' or source.get('media_type') not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
                    raise ValueError('Only authorized inline image bytes are enabled')
                model_input({'input': [{'type': 'input_image', 'image_url':
                                        'data:' + source['media_type'] + ';base64,' + source.get('data', '')}]})
            stack.extend((x, depth + 1) for x in value.values())


class ChatCompletionsAdapter(ModelAdapter):
    protocol = 'chat_completions'
    path = '/chat/completions'

    def conversation(self,instructions,text,images=()):
        content=[{'type':'text','text':text}]+[{'type':'image_url','image_url':{'url':image}} for image in images]
        return {'messages':[{'role':'system','content':instructions},{'role':'user','content':content}],'stream':False}

    def response_text(self,payload):
        choice=payload['choices'][0]
        if choice.get('finish_reason')!='stop':raise ValueError('Response is not complete')
        value=choice['message']['content']
        if not isinstance(value,str):raise ValueError('Response text is missing')
        return value

    def public_commentary(self,payload):
        try:value=payload['choices'][0]['message'].get('content')
        except (KeyError,IndexError,TypeError):return []
        return [value] if isinstance(value,str) and value.strip() else []

    def prepare(self, body, model, max_output_tokens, reasoning_effort=None):
        value = copy.deepcopy(body)
        validate_messages(value.get('messages'))
        allowed = {'messages', 'tools', 'tool_choice', 'parallel_tool_calls', 'stream', 'temperature',
                   'top_p', 'stop', 'response_format', 'reasoning_effort', 'thinking'}
        result = {k: v for k, v in value.items() if k in allowed}
        tools = result.get('tools', [])
        if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('type') != 'function' for t in tools):
            raise ValueError('Only client function tools are enabled')
        from .egress_policy import model_input
        model_input({'tools': tools})
        result['model'] = model
        result['max_tokens'] = bounded_output(value.get('max_tokens', value.get('max_completion_tokens')), max_output_tokens)
        if reasoning_effort:result['reasoning_effort']=reasoning_effort
        if result.get('stream'):
            result['stream_options'] = {'include_usage': True}
        return result

    def receipt(self):
        return ChatReceipt()


class AnthropicAdapter(ModelAdapter):
    protocol = 'anthropic'
    path = '/messages'

    def input_token_request(self,body):
        return '/messages/count_tokens',{k:body[k] for k in ('model','system','messages','tools','tool_choice','thinking') if k in body}

    def conversation(self,instructions,text,images=()):
        content=[{'type':'text','text':text}]+[{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':image.split(',',1)[1]}} for image in images]
        return {'system':instructions,'messages':[{'role':'user','content':content}],'stream':False}

    def response_text(self,payload):
        if payload.get('stop_reason') not in ('end_turn','stop_sequence'):raise ValueError('Response is not complete')
        texts=[c.get('text','') for c in payload.get('content',[]) if c.get('type')=='text']
        if len(texts)!=1 or not isinstance(texts[0],str):raise PublicResponseStructureError('Structured Agent requires exactly one public text block')
        return texts[0]

    def public_commentary(self,payload):
        return [item['text'] for item in payload.get('content',[]) if isinstance(item,dict) and item.get('type')=='text' and isinstance(item.get('text'),str) and item['text'].strip()]

    def headers(self, key):
        return {**super().headers(key), 'x-api-key': key, 'anthropic-version': '2023-06-01'}

    def prepare(self, body, model, max_output_tokens, reasoning_effort=None):
        value = copy.deepcopy(body)
        validate_messages(value.get('messages'))
        validate_messages([{'role': 'system', 'content': value.get('system', '')}])
        allowed = {'messages', 'system', 'tools', 'tool_choice', 'stream', 'temperature', 'top_p',
                   'top_k', 'stop_sequences', 'thinking', 'output_config', 'metadata'}
        result = {k: v for k, v in value.items() if k in allowed}
        tools = result.get('tools', [])
        if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('type', 'custom') != 'custom' for t in tools):
            raise ValueError('Provider-executed tools are not enabled')
        from .egress_policy import model_input
        model_input({'input': tools})
        result['model'] = model
        result['max_tokens'] = bounded_output(value.get('max_tokens'), max_output_tokens)
        if reasoning_effort:result['output_config']={**(result.get('output_config') or {}),'effort':reasoning_effort}
        return result

    def receipt(self):
        return AnthropicReceipt()


class ChatReceipt(Receipt):
    def accept(self, data):
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict):
            return
        if isinstance(value.get('id'), str):
            self.response_id = value['id'][:200]
        usage = value.get('usage')
        if isinstance(usage, dict):
            details = usage.get('prompt_tokens_details') or {}
            output = usage.get('completion_tokens_details') or {}
            self.usage = {
                'input_tokens': number(usage.get('prompt_tokens')),
                'output_tokens': number(usage.get('completion_tokens')),
                'total_tokens': number(usage.get('total_tokens')),
                'cached_tokens': number(usage.get('prompt_cache_hit_tokens', details.get('cached_tokens'))),
                'reasoning_tokens': number(output.get('reasoning_tokens')),
            }
        for choice in value.get('choices', []):
            content = choice.get('delta', choice.get('message', {})) or {}
            callback = getattr(self, 'on_activity', None)
            if callback:
                if content.get('content'):
                    callback('responding')
                elif content.get('tool_calls'):
                    callback('prepare_tool')
                elif content.get('reasoning_content'):
                    callback('analysis')
            reason = choice.get('finish_reason')
            if reason:
                self.status = 'succeeded' if reason in ('stop', 'tool_calls') else 'incomplete'
        if value.get('error'):
            self.status = 'failed'


class AnthropicReceipt(Receipt):
    def __init__(self):
        super().__init__()
        self.native_usage = {}
        self.has_output_receipt = False
        self.reasoning_tokens = None
        self.observed_output=False
        self.usage_anomaly=''

    def accept(self, data):
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict):
            return
        message = value.get('message') if isinstance(value.get('message'), dict) else value
        content=message.get('content',[])
        if isinstance(content,list) and any(isinstance(c,dict) and (
            c.get('type')=='tool_use' or any(c.get(k) for k in ('text','thinking'))) for c in content):self.observed_output=True
        part=value.get('content_block') or value.get('delta') or {}
        if isinstance(part,dict) and (part.get('type')=='tool_use' or any(part.get(k) for k in ('text','thinking','partial_json'))):self.observed_output=True
        if isinstance(message.get('id'), str):
            self.response_id = message['id'][:200]
        usage = message.get('usage')
        if isinstance(usage, dict):
            # Message deltas contain cumulative output usage, not increments.
            self.native_usage.update({k: number(v) for k, v in usage.items() if number(v) is not None})
            if value.get('type')!='message_start' and number(usage.get('output_tokens')) is not None:
                self.has_output_receipt=True
                details=usage.get('output_tokens_details')
                if isinstance(details,dict):self.reasoning_tokens=number(details.get('thinking_tokens',details.get('reasoning_tokens')))
        native = self.native_usage
        if native:
            fresh = native.get('input_tokens')
            cached = native.get('cache_read_input_tokens', 0)
            created = native.get('cache_creation_input_tokens', 0)
            total_input = fresh + cached + created if fresh is not None else None
            output = native.get('output_tokens') if self.has_output_receipt else None
            self.usage_anomaly='model_usage_zero_output' if self.observed_output and output==0 else ''
            self.usage = {'input_tokens': total_input, 'output_tokens': output,
                          # Let the common ledger label totals derived from
                          # input/output; do not fabricate a provider receipt.
                          'total_tokens': native.get('total_tokens'),
                          'cached_tokens': native.get('cache_read_input_tokens'), 'reasoning_tokens': self.reasoning_tokens}
        delta = value.get('delta') or {}
        callback = getattr(self, 'on_activity', None)
        if callback:
            if delta.get('type') == 'text_delta':
                callback('responding')
            elif delta.get('type') == 'thinking_delta':
                callback('analysis')
            elif delta.get('type') == 'input_json_delta':
                callback('prepare_tool')
        reason = message.get('stop_reason') or delta.get('stop_reason')
        if reason:
            self.status = 'succeeded' if reason in ('end_turn', 'tool_use', 'stop_sequence') else 'incomplete'
        if value.get('type') == 'error' or value.get('error'):
            self.status = 'failed'


REGISTRY = {a.protocol: a for a in (ResponsesAdapter(), ChatCompletionsAdapter(), AnthropicAdapter())}


def get(protocol):
    try:
        return REGISTRY[protocol]
    except KeyError:
        raise ValueError('Unsupported model wire protocol') from None
