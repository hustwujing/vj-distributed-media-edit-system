"""Translate only public SDK events; never read native conversation files."""
import json
import time
import uuid


def text_blocks(blocks):
    return ''.join(str(b.get('text', '')) for b in blocks if isinstance(b, dict) and b.get('type') == 'text')


def tool_item(identity, name, arguments):
    item = {'id': identity, 'type': 'dynamicToolCall', 'tool': name}
    if name in ('Bash', 'bash', 'shell', 'exec_command', 'shell_execute'):
        item.update(type='commandExecution', command=str(arguments.get('command', arguments.get('cmd', ''))))
    elif name in ('Write', 'Edit', 'MultiEdit', 'write_file', 'edit_file', 'apply_patch'):
        item.update(type='fileChange', changes=[{'path': str(arguments.get('file_path', arguments.get('path', '')))}])
    return item


class SDKEvents:
    def __init__(self, emit):
        self.emit = emit
        self.message_id = ''
        self.last_text = ''
        self.last_message_id = ''
        self.last_error = None
        self.tools = {}

    def claude_stream(self, value):
        kind = value.get('type')
        if kind == 'message_start':
            self.message_id = value['message']['id']
        elif kind == 'content_block_delta' and value.get('delta', {}).get('type') == 'text_delta':
            self.emit('message.delta', self.message_id, {'delta': value['delta']['text']})

    def claude_message(self, blocks):
        text = text_blocks(blocks)
        if text:
            identity = self.message_id or uuid.uuid4().hex
            self.emit('message.completed', identity, {'text': text, 'phase': 'commentary'})
            self.message_id = ''
            self.last_text = text
            self.last_message_id = identity

    def claude_hook(self, value):
        name = value['tool_name']
        identity = value['tool_use_id']
        kind = value['hook_event_name']
        item = self.tools.get(identity) or tool_item(identity, name, value.get('tool_input', {}))
        self.tools[identity] = item
        if kind == 'PreToolUse':
            self.emit('tool.started', identity, {'item': item, 'startedAtMs': int(time.time() * 1000)})
        else:
            failed = kind == 'PostToolUseFailure'
            output = value.get('error') if failed else value.get('tool_response')
            result = dict(item, status='failed' if failed else 'completed',
                          aggregatedOutput=json.dumps(output, ensure_ascii=False, default=str)[:4000])
            self.emit('tool.completed', identity, {'item': result, 'completedAtMs': int(time.time() * 1000)})

    def deepseek(self, notification, root):
        if notification.method != 'session.event':
            return
        payload = notification.payload
        if payload.get('sessionId') != root:
            return
        event = payload.get('event', {})
        kind = event.get('type')
        data = event.get('data', {})
        if kind == 'assistant/message':
            message = data.get('message', data)
            text = text_blocks(message.get('content', []))
            if text:
                self.last_text = text
                self.last_message_id = str(message.get('id', f'{root}:{event.get("seq")}'))
                self.emit('message.completed', self.last_message_id,
                          {'text': text, 'phase': 'commentary'})
            if data.get('usage'):
                self.emit('usage.reported', str(event.get('seq', '')),
                          {'usage': data['usage'], 'scope': 'sdk_step_evidence'})
        elif kind == 'tool/call':
            arguments = data.get('arguments', '{}')
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                arguments = {}
            identity = data['callId']
            item = tool_item(identity, data['name'], arguments if isinstance(arguments, dict) else {})
            self.tools[identity] = item
            self.emit('tool.started', identity, {'item': item, 'startedAtMs': event.get('time', int(time.time() * 1000))})
        elif kind == 'tool/result':
            message = data.get('message', data)
            identity = message.get('callId', message.get('toolCallId', data.get('callId')))
            if not identity:
                raise ValueError('SDK tool result has no public correlation ID')
            item = dict(self.tools.get(identity) or tool_item(identity, 'tool', {}))
            item.update(status='failed' if message.get('isError') else 'completed',
                        aggregatedOutput=text_blocks(message.get('content', []))[:4000])
            self.emit('tool.completed', identity, {'item': item, 'completedAtMs': event.get('time', int(time.time() * 1000))})
        elif kind == 'turn/end':
            reason=data.get('reason',{})
            if reason.get('kind')!='completed':
                error=reason.get('error') or {}
                self.last_error={'code':error.get('code',reason.get('kind','unknown')),
                                 'message':str(error.get('message',reason.get('kind','未完成')))[:1600]}

    def finish(self, text, status, error=None):
        if status=='completed':
            text=text or self.last_text
        if text:
            identity=self.last_message_id if text==self.last_text else uuid.uuid4().hex
            self.emit('message.completed', identity, {'text': text, 'phase': 'final_answer'})
        self.emit('turn.ended', '', {'status': status, 'error': error})
