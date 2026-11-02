"""Public process protocols behind a common Agent lifecycle and event contract."""
from dataclasses import dataclass, asdict
from importlib.metadata import version, PackageNotFoundError
from .harness_events import AgentEvent


@dataclass(frozen=True)
class HarnessCapabilities:
    light_conversation: bool = False
    streaming_text: bool = True
    tool_events: bool = True
    native_resume: bool = True
    interrupt: bool = True
    opaque_checkpoint: bool = True
    public_context: bool = True
    interactive_user_input: bool = False
    interrupt_mechanism: str = 'runtime_process_stop_and_fenced_cleanup'
    tool_event_scope: str = 'root_session_only'
    sdk_usage_scope: str = 'response_or_run_evidence_only'


class HarnessAdapter:
    id = ''
    version = 1
    capabilities = HarnessCapabilities()

    def initialize(self, rpc, write):
        rpc('initialize', {'clientInfo': {'name': 'video_use_studio', 'version': '0.2.0'}})
        write({'method': 'initialized', 'params': {}})

    def open_session(self, rpc, profile, instructions, session_id=None):
        result = rpc('session/open', {'session_id': session_id, 'model': profile.model,
                                    'instructions': instructions, 'cwd': '/workspace'})
        return result['session_id']

    def run_turn(self, rpc, session_id, text, profile):
        return rpc('turn/start', {'session_id': session_id, 'text': text,
                                  'effort': profile.reasoning_effort})

    def respond(self, write, message):
        # Native permission requests are never blanket-approved by the host.
        write({'id': message['id'], 'error': {'code': -32601, 'message': 'Interaction is not authorized'}})

    def interrupt(self, rpc, session_id):
        return rpc('turn/interrupt', {'session_id': session_id})

    def events(self, message):
        if message.get('method') == 'agent/event':
            value = message['params']
            return [AgentEvent(value['kind'], value.get('id', ''), value.get('data', {}))]
        return []

    def launch(self):
        return ['/opt/venv/bin/python', '/opt/sandbox_entry.py', '--command',
                '/opt/venv/bin/python', '/opt/harness_bridge.py', self.id, '/opt/harness-config.json']

    def available(self):
        return True


class CodexAdapter(HarnessAdapter):
    id = 'codex'
    capabilities = HarnessCapabilities(light_conversation=True)

    def open_session(self, rpc, profile, instructions, session_id=None):
        params = {'cwd': '/workspace', 'model': profile.model, 'approvalPolicy': 'never',
                  'sandbox': 'danger-full-access', 'developerInstructions': instructions}
        if session_id:
            params['threadId'] = session_id
        return rpc('thread/resume' if session_id else 'thread/start', params)['thread']['id']

    def run_turn(self, rpc, session_id, text, profile):
        return rpc('turn/start', {'threadId': session_id, 'cwd': '/workspace', 'approvalPolicy': 'never',
                                  'sandboxPolicy': {'type': 'externalSandbox', 'networkAccess': 'enabled'},
                                  'input': [{'type': 'text', 'text': text}], 'effort': profile.reasoning_effort})

    def respond(self, write, message):
        method = message.get('method', '')
        if 'requestApproval' in method:
            result = {'permissions': {}} if 'permissions' in method else {'decision': 'decline'}
        elif 'requestUserInput' in method:
            result = {'answers': {}}
        else:
            return super().respond(write, message)
        write({'id': message['id'], 'result': result})

    def interrupt(self, rpc, session_id):
        return rpc('turn/interrupt', {'threadId': session_id})

    def launch(self):
        return ['/opt/venv/bin/python', '/opt/sandbox_entry.py', '/opt/codex']

    def events(self, message):
        method = message.get('method', '')
        params = message.get('params', {})
        if method == 'item/agentMessage/delta':
            return [AgentEvent('message.delta', params['itemId'], {'delta': params.get('delta', '')})]
        if method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            identity = item.get('id', '')
            if item.get('type') == 'agentMessage' and method == 'item/completed':
                return [AgentEvent('message.completed', identity,
                                   {'text': item.get('text', ''), 'phase': item.get('phase', 'final_answer')})]
            if item.get('type') in ('commandExecution', 'fileChange', 'imageView', 'mcpToolCall', 'dynamicToolCall'):
                allowed = ('id', 'type', 'command', 'path', 'changes', 'tool', 'status', 'exitCode', 'aggregatedOutput', 'durationMs')
                payload = {'item': {key: item[key] for key in allowed if key in item}}
                payload.update({key: params[key] for key in ('startedAtMs', 'completedAtMs') if key in params})
                return [AgentEvent('tool.started' if method == 'item/started' else 'tool.completed', identity, payload)]
        if method in ('item/commandExecution/outputDelta', 'item/fileChange/outputDelta'):
            return [AgentEvent('tool.output', params.get('itemId', ''), {'delta': params.get('delta', '')})]
        if method == 'turn/plan/updated':
            return [AgentEvent('plan.updated', data={'plan': params.get('plan', [])})]
        if method == 'turn/completed':
            turn = params.get('turn', {})
            return [AgentEvent('turn.ended', turn.get('id', ''),
                               {'status': turn.get('status', 'failed'), 'error': turn.get('error')})]
        if method == 'error':
            return [AgentEvent('error', data={'message': '模型连接报告异常，正在处理。'})]
        return []  # Reasoning and undocumented native events are not public context.


class ClaudeCodeAdapter(HarnessAdapter):
    id = 'claude_code'

    def available(self):
        try:
            return version('claude-agent-sdk') == '0.2.176'
        except PackageNotFoundError:
            return False


class DeepSeekHarnessAdapter(HarnessAdapter):
    id = 'deepseek_harness'
    # The pinned SDK reliably publishes completed assistant messages. Do not
    # advertise token-level deltas until that public protocol is verified.
    capabilities = HarnessCapabilities(streaming_text=False)

    def available(self):
        try:
            return version('deepseek-harness-sdk') == '0.2.1rc2'
        except PackageNotFoundError:
            return False


REGISTRY = {adapter.id: adapter for adapter in (CodexAdapter(), ClaudeCodeAdapter(), DeepSeekHarnessAdapter())}


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError('Unsupported harness adapter') from None


def catalog():
    return [{'id': a.id, 'version': a.version, 'available': a.available(),
             'capabilities': asdict(a.capabilities)} for a in REGISTRY.values()]


def can_use_light(store, task):
    """Admission and dispatch must agree on whether a sandbox is required."""
    from .plan_readiness import is_light
    from .agent_profiles import for_task
    return is_light(store, task) and get(for_task(store, task).harness).capabilities.light_conversation
