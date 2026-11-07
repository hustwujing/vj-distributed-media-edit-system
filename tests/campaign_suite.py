"""Run and report all isolated dynamic workflow live cases; never target main."""
import argparse,json,os,subprocess,sys,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parents[1]
DEPLOY_ROOT=Path(os.environ.get('DYNAMIC_CASE_DEPLOY_ROOT',ROOT/'.cluster')).resolve()
SESSION_COOKIE=os.environ.get('DYNAMIC_SESSION_COOKIE','dynamic_agent_session')
RISK_ORDER={'complex':0,'intervention_real_browser':1,'intervention':1,'dynamic_protocol':2,'simple':3}
def order_cases(cases,mode):
 if mode=='manifest':return list(cases)
 return sorted(cases,key=lambda case:RISK_ORDER.get(case.get('complexity'),99))

def validate_deployment_roots(parser):
 cluster_root=Path(os.environ.get('DAS_DEPLOY_ROOT',ROOT/'.cluster')).resolve()
 if cluster_root!=DEPLOY_ROOT:
  parser.error('DYNAMIC_CASE_DEPLOY_ROOT and DAS_DEPLOY_ROOT must identify the same isolated deployment')

def request(base,cookie,method,path,payload=None):
 data=json.dumps(payload).encode() if payload is not None else None
 headers={'Cookie':SESSION_COOKIE+'='+cookie}
 if data is not None:headers['Content-Type']='application/json'
 try:
  with urlopen(Request(base+path,data=data,headers=headers,method=method),timeout=30) as response:
   return response.status,json.loads(response.read() or b'{}')
 except HTTPError as error:
  try:body=json.loads(error.read() or b'{}')
  except (TypeError,ValueError):body={}
  return error.code,body

def probe(directory,action):
 script=directory/'harness/campaign_dynamic_probe.py'
 if not script.exists():return None
 environment={**os.environ,'PYTHONPATH':str(ROOT),'DYNAMIC_CAMPAIGN_REPO':str(ROOT)}
 result=subprocess.run([sys.executable,str(script),action,str(directory)],cwd=ROOT,env=environment,capture_output=True,text=True,timeout=600)
 if result.returncode:return None
 try:return json.loads(result.stdout.strip().splitlines()[-1])
 except (IndexError,ValueError):return None

def interrupt_attempt(base,state_file):
 directory=state_file.parent;storage=directory/'browser-state.json'
 if not storage.exists():return False
 state=json.loads(state_file.read_text());cookies=json.loads(storage.read_text()).get('cookies',[])
 cookie=next((item.get('value') for item in cookies if item.get('name')==SESSION_COOKIE),None)
 project=state.get('project');runs=list(dict.fromkeys(state.get('run_ids') or [state.get('run_id')]))
 if not cookie or not project or not any(runs):return False
 for run in filter(None,runs):request(base,cookie,'POST',f'/api/projects/{project}/runs/{run}/commands',{'request_id':uuid.uuid4().hex,'action':'cancel'})
 deadline=time.monotonic()+180
 while time.monotonic()<deadline:
  pending=[]
  for run in filter(None,runs):
   status,body=request(base,cookie,'GET',f'/api/projects/{project}/runs/{run}')
   active=[item for item in [*body.get('tasks',[]),*body.get('control_jobs',[])] if item.get('status') in ('ready','leased','running','waiting','retry_wait','reviewing')]
   if status==200 and (body.get('state') not in ('SUCCEEDED','FAILED','CANCELLED') or body.get('cleanup_pending') or active):pending.append(run)
  if not pending:break
  time.sleep(1)
 internal=probe(directory,'final');full_audit=directory/'internal-final.json'
 if full_audit.exists():
  try:internal=json.loads(full_audit.read_text())
  except (TypeError,ValueError):pass
 unresolved=[]
 snapshot=directory/'internal-snapshot.json'
 if snapshot.exists():
  try:unresolved=[row for row in json.loads(snapshot.read_text())['dimensions']['external_cleanup']['records'] if row['state'] in ('unknown','unconfirmed','unsupported')]
  except (KeyError,TypeError,ValueError):unresolved=['audit_unavailable']
 dimensions=(internal or {}).get('dimensions',{});local_records=dimensions.get('local_cleanup',{}).get('records',[])
 safe_interrupt_audit=bool(internal and internal.get('program_passed')
  and dimensions.get('database',{}).get('checked') and dimensions.get('boss',{}).get('complete')
  and dimensions.get('external_cleanup',{}).get('checked')
  and dimensions.get('local_cleanup',{}).get('checked') and all(row.get('state')=='complete' for row in local_records))
 cleanup=None
 if safe_interrupt_audit and not unresolved:
  request(base,cookie,'DELETE',f'/api/projects/{project}')
  cleanup=probe(directory,'cleanup')
 state.update(status='finished',outcome='user_stopped',finished_at=time.time(),interrupted_by='SIGINT')
 if internal:state['internal']=internal
 if cleanup:state['cleanup']=cleanup
 if unresolved:state['cleanup_retained']='External state is unresolved after campaign interruption'
 state_file.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n')
 return bool(cleanup and cleanup.get('complete')) or bool(unresolved)

def run_browser(cases,base,jobs):
 existing=set((DEPLOY_ROOT/'campaigns/dynamic-plan/runs').glob('*/attempt-*/state.json'))
 command=['node',str(ROOT/'tests/campaign_dynamic_browser.cjs'),','.join(c['id'] for c in cases)]
 process=subprocess.Popen(command,cwd=ROOT,env={**os.environ,'DYNAMIC_CASE_BASE':base,'DYNAMIC_CASE_JOBS':str(jobs),
  'DYNAMIC_CASE_DEPLOY_ROOT':str(DEPLOY_ROOT)})
 try:return process.wait()
 except KeyboardInterrupt:
  pause=DEPLOY_ROOT/'campaigns/dynamic-plan/pause-new-cases';pause.write_text('Campaign interrupted; parent recovery is cancelling and cleaning active attempts\n')
  if process.poll() is None:process.terminate()
  try:process.wait(timeout=10)
  except subprocess.TimeoutExpired:process.kill();process.wait()
  current=set((DEPLOY_ROOT/'campaigns/dynamic-plan/runs').glob('*/attempt-*/state.json'))
  for state_file in sorted(current-existing):interrupt_attempt(base,state_file)
  return 130
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('action',choices=['list','run','report'])
 p.add_argument('--group',choices=['all','original','intervention','dynamic'],default='all')
 p.add_argument('--cases',default='all');p.add_argument('--repeat',type=int,default=1)
 p.add_argument('--jobs',type=int,choices=[1,2,3,4,5,6],default=1)
 p.add_argument('--order',choices=['risk','manifest'],default='risk')
 p.add_argument('--base',default='http://127.0.0.1/distributed-agent-system')
 a=p.parse_args()
 validate_deployment_roots(p)
 expected=os.environ.get('DYNAMIC_EXPECT_PATH','/distributed-agent-system')
 if urlsplit(a.base).path.rstrip('/')!=expected:p.error('Only the configured isolated deployment path is allowed')
 manifest=json.loads((ROOT/'docs/real-case-campaign/dynamic-cases.json').read_text())
 if a.action=='run':
  release=json.loads((DEPLOY_ROOT/'release.json').read_text())
  if release.get('state')!='ready' or not release.get('config_sha256'):p.error('Campaign requires a ready release with frozen configuration')
  if a.cases=='all' and release.get('verification')!='full_gate':p.error('A full campaign requires a release marked verification=full_gate')
  if release.get('namespace')!=os.environ.get('DYNAMIC_EXPECT_NAMESPACE','dynamic-agent'):
   p.error('Campaign deployment namespace mismatch')
 cases=[c for c in manifest['cases'] if a.group=='all' or (a.group=='original' and c['id'][0] in 'SC') or (a.group=='intervention' and c['id'].startswith('R')) or (a.group=='dynamic' and c['id'].startswith('D'))]
 if a.cases!='all':
  wanted=set(a.cases.split(','));cases=[c for c in cases if c['id'] in wanted]
  if {c['id'] for c in cases}!=wanted:p.error('Case IDs do not belong to the selected group')
 cases=order_cases(cases,a.order)
 if a.action=='list':print(json.dumps([{'id':c['id'],'title':c['title']} for c in cases],ensure_ascii=False,indent=2));return 0
 if a.action=='report':return subprocess.call([sys.executable,str(ROOT/'tests/campaign_dynamic_report.py')],cwd=ROOT)
 if not 1<=a.repeat<=10:p.error('repeat must be 1..10')
 failed=False
 for _ in range(a.repeat):
  failed|=bool(run_browser(cases,a.base,a.jobs))
 subprocess.call([sys.executable,str(ROOT/'tests/campaign_dynamic_report.py')],cwd=ROOT)
 return int(failed)
if __name__=='__main__':raise SystemExit(main())
