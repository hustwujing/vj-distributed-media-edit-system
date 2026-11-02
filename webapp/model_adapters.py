"""Native model wire protocols behind the same fenced, metered gateway.

Adapters neither own credentials nor retry requests. The gateway owns each
network attempt, cancellation, and the authoritative usage receipt.
"""
import base64
import copy
import json
from .usage_protocol import Receipt, number


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


class ResponsesAdapter(ModelAdapter):
    protocol = 'responses'
    path = '/responses'

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

    def accept(self, data):
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict):
            return
        message = value.get('message') if isinstance(value.get('message'), dict) else value
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
