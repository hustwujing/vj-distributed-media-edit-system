"""Real OpenSandbox control/session initialization; no model turns or user data."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid


class ProbeCleanup:
    def reserve(self,lease):pass
    def attach(self,lease,sandbox):pass
    def complete(self,store,attempt):pass


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    parser.add_argument('--harness',choices=('claude_code','codex','deepseek_harness'),required=True)
    parser.add_argument('--output',required=True);args=parser.parse_args()
    output=Path(args.output).resolve()
    if not output.is_relative_to('/data2/wujing03'):raise ValueError('Isolated evidence path required')
    from deploy.common import env_file
    env=env_file(Path('/etc/distributed-agent-sandbox-backend/opensandbox-runtime.env'))
    if env['VIDEO_USE_OPEN_SANDBOX_URL']!='http://127.0.0.1:28090':raise ValueError('Wrong lifecycle namespace')
    os.environ.update(env);os.environ['VIDEO_USE_MODEL_API_KEY']='probe-placeholder'
    from webapp.agent_profiles import AgentProfile
    from webapp.sandbox_backends import OpenSandboxClient,RequestsTransport,BackendBinding
    from webapp.opensandbox_sdk import OpenSandboxSDK
    from webapp.opensandbox_command import OpenSandboxCommandBackend
    from webapp.native_implementer_runtime import NativeImplementerRuntime
    lifecycle=OpenSandboxClient(RequestsTransport(env['VIDEO_USE_OPEN_SANDBOX_URL'],env['VIDEO_USE_OPEN_SANDBOX_API_KEY']),network_policy='internal_bridge')
    sdk=OpenSandboxSDK(env['VIDEO_USE_OPEN_SANDBOX_URL'],env['VIDEO_USE_OPEN_SANDBOX_API_KEY'])
    attempt=uuid.uuid4().hex;lease=SimpleNamespace(attempt=attempt,run_id=attempt,generation=1,
        store=None,start_compute=lambda:None,check=lambda:None,lost=__import__('threading').Event())
    binding=BackendBinding('opensandbox','probe-'+args.harness,args.image,
        env['VIDEO_USE_OPEN_SANDBOX_COMMAND_BUNDLE_DIGEST'],'agent_runtime')
    profile=AgentProfile(harness=args.harness,protocol='responses' if args.harness=='codex' else 'anthropic',
        model='gpt-6-astra' if args.harness=='codex' else 'claude-sonnet-5-5' if args.harness=='claude_code' else 'deepseek-v4.1-flash',
        base_url='https://provider.invalid',reasoning_effort='high')
    runtime=None;result={'passed':False,'harness':args.harness,'image':args.image,'model_requests':0}
    try:
        with tempfile.TemporaryDirectory(prefix='native-control-',dir='/data2/wujing03/vu-tmp') as folder:
            root=Path(folder);workspace=root/'workspace';sources=root/'sources'
            workspace.mkdir();sources.mkdir()
            runtime=OpenSandboxCommandBackend(lifecycle,sdk,ProbeCleanup(),runtime_type=NativeImplementerRuntime).start(
                lease,binding,{'cpu':2,'memory_mb':2048},timeout=180)
            result['sandbox_id']=runtime.sandbox_id
            runtime.env={'workspace':workspace,'sources':sources}
            runtime.start_harness(profile,binding,lambda event:None,task_context={
                'output_directory':'/workspace/edit/workflows/probe/task/out','inputs':{}})
            runtime.client.timeout=30
            session=runtime.client.call('session.open',{'instructions':'Initialization probe only; do not call any model or execute a turn.','session_id':None})
            result['session_opened']=bool(session.get('session_id'))
            runtime.freeze_harness();result['frozen']=True
            result['passed']=result['session_opened']
    except Exception as exc:
        result['error_type']=type(exc).__name__;result['error']=str(exc)[:1500]
        raise
    finally:
        if runtime:
            runtime.close()
            result['deleted']=runtime.sandbox_deleted
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':main()
