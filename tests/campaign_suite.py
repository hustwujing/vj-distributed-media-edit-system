"""Run and report all isolated dynamic workflow live cases; never target main."""
import argparse,json,os,subprocess,sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
RISK_ORDER={'complex':0,'intervention_real_browser':1,'intervention':1,'dynamic_protocol':2,'simple':3}
def order_cases(cases,mode):
 if mode=='manifest':return list(cases)
 return sorted(cases,key=lambda case:RISK_ORDER.get(case.get('complexity'),99))
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('action',choices=['list','run','report'])
 p.add_argument('--group',choices=['all','original','intervention','dynamic'],default='all')
 p.add_argument('--cases',default='all');p.add_argument('--repeat',type=int,default=1)
 p.add_argument('--jobs',type=int,choices=[1,2,3,4,5,6],default=1)
 p.add_argument('--order',choices=['risk','manifest'],default='risk')
 p.add_argument('--base',default='http://127.0.0.1/distributed-agent-system')
 a=p.parse_args()
 if urlsplit(a.base).path.rstrip('/')!='/distributed-agent-system':p.error('Only the isolated deployment path is allowed')
 manifest=json.loads((ROOT/'docs/real-case-campaign/dynamic-cases.json').read_text())
 if a.action=='run' and a.cases=='all':
  release=json.loads((ROOT/'.cluster/release.json').read_text())
  if release.get('verification')!='full_gate':p.error('A full campaign requires a release marked verification=full_gate')
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
  failed|=bool(subprocess.call(['node',str(ROOT/'tests/campaign_dynamic_browser.cjs'),','.join(c['id'] for c in cases)],cwd=ROOT,
   env={**os.environ,'DYNAMIC_CASE_BASE':a.base,'DYNAMIC_CASE_JOBS':str(a.jobs)}))
 subprocess.call([sys.executable,str(ROOT/'tests/campaign_dynamic_report.py')],cwd=ROOT)
 return int(failed)
if __name__=='__main__':raise SystemExit(main())
