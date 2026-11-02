"""Unified recurring entry for the original 60 cases, R01–R16 reuse and R17 replan."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['list','run','report','cleanup','replan'])
 p.add_argument('--group',choices=['all','original','intervention','replan'],default='intervention');p.add_argument('--cases',default='all')
 p.add_argument('--repeat',type=int,default=1);p.add_argument('--jobs',type=int,choices=[1,2],default=1)
 p.add_argument('--run-id');p.add_argument('--base',default='http://10.194.49.8/video-use')
 p.add_argument('--approve-original-plans',action='store_true',help='Enable the existing reviewed-contract approval path for original browser cases')
 a=p.parse_args();manifest=json.loads((ROOT/'docs/real-case-campaign/cases.json').read_text())
 cases=[c for c in manifest['cases'] if a.group=='all' or (a.group=='replan' and c['id']=='R17') or (a.group=='intervention' and c['id'].startswith('R') and c['id']!='R17') or (a.group=='original' and not c['id'].startswith('R'))]
 if a.cases!='all':
  wanted=set(a.cases.split(','));cases=[c for c in cases if c['id'] in wanted]
  if {c['id'] for c in cases}!=wanted:p.error('Case IDs do not belong to the selected group')
 if a.action=='list':print(json.dumps([{'id':c['id'],'title':c['title'],'group':c['complexity']} for c in cases],ensure_ascii=False,indent=2));return 0
 if a.action=='replan':
  return subprocess.call(['node',str(ROOT/'tests/campaign_replan_real.cjs')],cwd=ROOT,env={**os.environ,'VU_CASE_BASE':a.base})
 if a.action=='report':return subprocess.call([sys.executable,str(ROOT/'tests/campaign_report.py')],cwd=ROOT)
 if a.action=='cleanup':
  if not a.run_id:p.error('cleanup requires --run-id')
  for state_path in (ROOT/'.cluster/campaigns/20261031-real60/runs/R17').glob('attempt-*/state.json'):
   if json.loads(state_path.read_text()).get('run_id')==a.run_id:
    return subprocess.call([sys.executable,str(ROOT/'tests/campaign_replan_cleanup.py'),str(state_path.parent),'--base',a.base],cwd=ROOT)
  return subprocess.call([sys.executable,str(ROOT/'tests/campaign_interventions.py'),'cleanup','--run-id',a.run_id,'--base',a.base],cwd=ROOT)
 if not 1<=a.repeat<=10:p.error('repeat must be 1..10')
 failed=False
 for _ in range(a.repeat):
  originals=[c['id'] for c in cases if not c['id'].startswith('R')]
  additions=[c['id'] for c in cases if c['id'].startswith('R') and c['id']!='R17']
  if originals:
   env={**os.environ,'VU_CASE_BASE':a.base,'VU_CASE_RERUN':'1','VU_CASE_CONCURRENCY':str(a.jobs)}
   if a.approve_original_plans:env['VU_CASE_AUTO_APPROVE']='1'
   failed|=bool(subprocess.call(['node',str(ROOT/'tests/real_case_browser.cjs'),','.join(originals)],cwd=ROOT,env=env))
  if additions:failed|=bool(subprocess.call([sys.executable,str(ROOT/'tests/campaign_interventions.py'),'run','--cases',','.join(additions),'--jobs',str(a.jobs),'--base',a.base],cwd=ROOT))
  if any(c['id']=='R17' for c in cases):failed|=bool(subprocess.call(['node',str(ROOT/'tests/campaign_replan_real.cjs')],cwd=ROOT,env={**os.environ,'VU_CASE_BASE':a.base}))
 return int(failed)
if __name__=='__main__':raise SystemExit(main())
