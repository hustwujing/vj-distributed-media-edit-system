"""Trusted SDK bridge running INSIDE the project's OS sandbox.

Only public SDK lifecycle and event APIs are used. The loopback gateway holds
all actual credentials and enforces the frozen model route and budgets.
"""
import asyncio
import dataclasses
import json
import os
import sys
import threading
import uuid
from pathlib import Path

try:
    from .harness_sdk_events import SDKEvents
except ImportError:
    from harness_sdk_events import SDKEvents


class Bridge:
    def __init__(self, harness, config, output):
        self.harness, self.config, self.output = harness, config, output
        if config.get('response_transport') not in ('buffered','stream'):
            raise ValueError('Invalid native Harness response transport')
        self.lock = threading.Lock()
        self.session_id = None
        self.instructions = ''
        self.resume = False
        self.busy = False
        self.sdk = None
        self.codec = SDKEvents(self.emit)

    def write(self, value):
        with self.lock:
            self.output.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.output.flush()

    def emit(self, kind, identity, data):
        self.write({'method': 'agent/event', 'params': {'kind': kind, 'id': identity, 'data': data}})

    def open_session(self, params):
        if self.busy:
            raise ValueError('Cannot replace a running session')
        self.instructions = params['instructions']
        self.resume = bool(params.get('session_id'))
        self.session_id = params.get('session_id') or str(uuid.uuid4())
        if self.harness == 'claude_code':
            from claude_agent_sdk import get_session_info
            os.environ['CLAUDE_CONFIG_DIR'] = '/state/claude'
            if self.resume and get_session_info(self.session_id, directory='/workspace') is None:
                raise ValueError('Native session is unavailable')
        elif self.harness == 'deepseek_harness':
            from deepseek_harness import DeepSeekHarness
            if self.sdk:
                self.sdk.close()
            self.sdk = DeepSeekHarness(dsh_home='/state/dsh', cwd='/workspace',
                                      provider='deepseek-official', model=self.config['model'],
                                      reasoning_effort=self.config['reasoning_effort'],
                                      max_tokens=self.config['max_output_tokens'],
                                      base_url='http://127.0.0.1:18766/v1', api_key='gateway-placeholder',
                                      # This bridge already runs after UID drop in
                                      # the application filesystem/network/cgroup
                                      # sandbox. Native nesting is unavailable.
                                      env={**self.runtime_environment(),'DSH_PERMISSION_MODE':'danger-full-access',
                                           'PYTHONPATH':'/opt/video-use:/opt/video-use/helpers'},
                                      request_timeout_seconds=30, shutdown_timeout_seconds=5)
            self.sdk.start()
            if self.resume:
                self.sdk.resume_session(self.session_id)
        else:
            raise ValueError('Unsupported SDK bridge')
        return {'session_id': self.session_id}

    @staticmethod
    def runtime_environment():
        names=('NODE_PATH','NUMBA_CACHE_DIR','PLAYWRIGHT_BROWSERS_PATH','PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD','U2NET_HOME')
        return {name:os.environ[name] for name in names if os.environ.get(name)}

    async def claude(self, text):
        from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher
        from claude_agent_sdk.types import StreamEvent, AssistantMessage, ResultMessage, SystemMessage

        async def hook(value, tool_use_id, context):
            self.codec.claude_hook(value)
            return {}

        hooks = {name: [HookMatcher(hooks=[hook])] for name in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure')}
        options = ClaudeAgentOptions(
            cwd='/workspace', model=self.config['model'], effort=self.config['reasoning_effort'],
            system_prompt=self.instructions, tools=['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep'],
            allowed_tools=['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep'],
            permission_mode='bypassPermissions', setting_sources=[], strict_mcp_config=True,
            include_partial_messages=self.config['response_transport']=='stream', hooks=hooks,
            resume=self.session_id if self.resume else None,
            session_id=None if self.resume else self.session_id,
            env={**self.runtime_environment(),'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18766', 'ANTHROPIC_API_KEY': 'gateway-placeholder',
                 'CLAUDE_CONFIG_DIR': '/state/claude', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                 'DISABLE_AUTOUPDATER': '1','PYTHONPATH':'/opt/video-use:/opt/video-use/helpers'},
        )
        outcome = None
        async for message in query(prompt=text, options=options):
            if isinstance(message, StreamEvent):
                self.codec.claude_stream(message.event)
            elif isinstance(message, AssistantMessage):
                self.codec.claude_message([dataclasses.asdict(block) | {'type': type(block).__name__.replace('Block', '').lower()}
                                          for block in message.content if type(block).__name__ == 'TextBlock'])
            elif isinstance(message, ResultMessage):
                outcome = message
                self.emit('usage.reported', '', {'usage': message.usage, 'scope': 'sdk_run_aggregate_evidence'})
        if outcome is None:
            raise RuntimeError('SDK ended without a result')
        # query() has exited and reaped the native subprocess before checkpoint.
        self.codec.finish(outcome.result or '', 'completed' if not outcome.is_error and outcome.subtype == 'success' else 'failed',
                          None if not outcome.is_error else outcome.subtype)

    def run(self, text):
        try:
            self.codec = SDKEvents(self.emit)
            if self.harness == 'claude_code':
                asyncio.run(self.claude(text))
            else:
                # No private session decoding. SDK emits public messages and tools.
                result = self.sdk.start_session(self.session_id).run(
                    self.instructions + '\n\n' + text,
                    on_notification=lambda value: self.codec.deepseek(value, self.session_id))
                self.sdk.close()
                self.sdk = None
                self.codec.finish(result.final_response, 'completed' if result.finish_reason == 'completed' else 'failed',
                                  None if result.finish_reason == 'completed' else self.codec.last_error or result.finish_reason)
        except BaseException as exc:
            if self.sdk:
                self.sdk.close()
                self.sdk = None
            self.emit('turn.ended', '', {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)[:1600]})
        finally:
            self.busy = False

    def dispatch(self, message):
        identity = message.get('id')
        method = message.get('method')
        if identity is None:
            return
        try:
            if method == 'initialize':
                result = {'name': self.harness, 'bridge_version': 1}
            elif method == 'session/open':
                result = self.open_session(message['params'])
            elif method == 'turn/start':
                if self.busy or not self.session_id or message['params']['session_id'] != self.session_id:
                    raise ValueError('Invalid or busy session')
                self.busy = True
                self.write({'id': identity, 'result': {'started': True}})
                threading.Thread(target=self.run, args=(message['params']['text'],), daemon=True).start()
                return
            elif method == 'turn/interrupt':
                # The host performs bounded SIGTERM/SIGKILL and cgroup cleanup.
                result = {'status': 'requires_process_stop', 'upstream_cancelled': False}
            else:
                raise ValueError('Unsupported bridge method')
            self.write({'id': identity, 'result': result})
        except Exception as exc:
            self.write({'id': identity, 'error': {'code': -32000, 'message': type(exc).__name__ + ': ' + str(exc)[:1600]}})


def main():
    config = json.loads(Path(sys.argv[2]).read_text())
    bridge = Bridge(sys.argv[1], config, sys.stdout)
    for line in iter(lambda: sys.stdin.readline(8 * 1024**2 + 1), ''):
        if len(line) > 8 * 1024**2:
            raise ValueError('Bridge control message exceeds limit')
        bridge.dispatch(json.loads(line))
    if bridge.sdk:
        bridge.sdk.close()


if __name__ == '__main__':
    main()
