"""Run the complete pinned Skill smoke inside one real OpenSandbox/gVisor sandbox."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid


class Cleanup:
    def reserve(self,lease):pass
    def attach(self,lease,sandbox):pass
    def complete(self,store,attempt):pass


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image');args=parser.parse_args();output=args.output.resolve()
    if not output.is_relative_to('/data2/wujing03'):raise ValueError('Skill evidence must stay under /data2/wujing03')
    from deploy.common import env_file
    env=env_file(Path('/etc/distributed-agent-sandbox-backend/opensandbox-runtime.env'))
    if env['VIDEO_USE_OPEN_SANDBOX_URL']!='http://127.0.0.1:28090':raise ValueError('Wrong OpenSandbox namespace')
    os.environ.update(env)
    image=args.image or env['VIDEO_USE_OPEN_SANDBOX_COMMAND_IMAGE_DIGEST']
    from webapp.sandbox_backends import OpenSandboxClient,RequestsTransport,BackendBinding
    from webapp.opensandbox_sdk import OpenSandboxSDK
    from webapp.opensandbox_command import OpenSandboxCommandBackend
    lifecycle=OpenSandboxClient(RequestsTransport(env['VIDEO_USE_OPEN_SANDBOX_URL'],
        env['VIDEO_USE_OPEN_SANDBOX_API_KEY']),network_policy='internal_bridge')
    sdk=OpenSandboxSDK(env['VIDEO_USE_OPEN_SANDBOX_URL'],env['VIDEO_USE_OPEN_SANDBOX_API_KEY'])
    attempt='skill-'+uuid.uuid4().hex;lease=SimpleNamespace(attempt=attempt,run_id=attempt,generation=1,
        store=None,start_compute=lambda:None,check=lambda:None,lost=__import__('threading').Event())
    binding=BackendBinding('opensandbox',env['VIDEO_USE_OPEN_SANDBOX_COMMAND_RUNTIME_PROFILE'],image,
        env['VIDEO_USE_OPEN_SANDBOX_COMMAND_BUNDLE_DIGEST'],'command_runtime')
    runtime=None;report={'passed':False,'image':image,'bundle_digest':binding.skill_digest,
        'sandbox_backend':'opensandbox','sandbox_runtime':'gvisor','network':'internal_bridge'}
    try:
        runtime=OpenSandboxCommandBackend(lifecycle,sdk,Cleanup()).start(lease,binding,
            {'cpu':4,'memory_mb':6144},timeout=1500)
        report['sandbox_id']=runtime.sandbox_id
        execution=runtime._run(['/usr/local/bin/sandbox-skill-smoke'],1200,lambda:False)
        evidence=json.loads(runtime._stdout(execution));report['evidence']=evidence
        expected={row['id'] for row in json.loads((Path(__file__).parents[1]/'runtime/skills/manifest.json').read_text())['skills']}
        actual=set(evidence.get('skills',{}));report['skill_count']=len(actual)
        report['missing_skills']=sorted(expected-actual);report['unexpected_skills']=sorted(actual-expected)
        report['passed']=bool(evidence.get('passed')) and actual==expected and set(evidence['harnesses'].values())=={25}
        if not report['passed']:raise AssertionError('Real Skill matrix did not cover the frozen manifest')
    except Exception as exc:
        report['error_type']=type(exc).__name__;report['error']=str(exc)[:2000];raise
    finally:
        if runtime:
            runtime.close();report['sandbox_deleted']=runtime.sandbox_deleted
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'passed':report['passed'],'skills':report.get('skill_count',0),
            'sandbox_deleted':report.get('sandbox_deleted',False),'output':str(output)},ensure_ascii=False))


if __name__=='__main__':main()
