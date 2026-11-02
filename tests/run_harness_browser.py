"""Real browser/API/PG/Redis switch control test; no paid model calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    from tests.isolation import isolate_environment,isolate_network
    isolate_environment(out/'environment');isolate_network()
    os.environ.update(VIDEO_USE_MODEL_BASE_URL='http://127.0.0.1:9/v1',VIDEO_USE_MODEL_API_KEY='isolated-test-only',
                      VIDEO_USE_CLAUDE_API_KEY='isolated-test-only',VIDEO_USE_AGENT_PROFILES_JSON=json.dumps({
                          'claude':{'harness':'claude_code','protocol':'anthropic','model':'browser-fixture',
                                    'base_url':'http://127.0.0.1:9/v1','credential_env':'VIDEO_USE_CLAUDE_API_KEY','reasoning_effort':'high'}}))
    from tests.test_api_pressure import APIPressureTests
    from webapp.harness_switching import reconcile
    case=APIPressureTests('test_two_apis_preserve_control_and_bound_all_slow_dependencies')
    stop=threading.Event();worker=None
    try:
        case.setUp();task=case.task()
        case.store.execute('INSERT INTO execution_workers(id,node,release,model,heartbeat,draining) VALUES (?,?,?,?,?,0)',
                           ('browser-fixture','browser-node','test','fixture',case.store.now()+3600))
        def control():
            while not stop.wait(.1):
                if (out/'reconcile').exists():reconcile(case.store,task)
        worker=threading.Thread(target=control);worker.start()
        config={'base':case.proxy,'project':case.project,'task':task,'output':str(out),
                'cookies':[{'name':c.name,'value':c.value,'domain':'127.0.0.1','path':'/'} for c in case.session.cookies]}
        path=out/'private-config.json';path.write_text(json.dumps(config));path.chmod(0o600)
        result=subprocess.run(['node','tests/browser_harness.cjs',str(path)],capture_output=True,text=True,timeout=90)
        print(result.stdout);print(result.stderr)
        if result.returncode:raise SystemExit(result.returncode)
        assert case.store.query('SELECT state FROM agent_harness_switches WHERE task_id=? ORDER BY created',(task,))==[{'state':'cancelled'},{'state':'activated'}]
        print(json.dumps({'passed':True,'report':str(out/'report.json'),'database_switches':['cancelled','activated']}))
    finally:
        stop.set()
        if worker:worker.join(timeout=5)
        case.doCleanups()


if __name__=='__main__':main()
