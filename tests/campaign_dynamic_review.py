"""Read-only compact evidence summaries for explicit per-case operator review."""
import argparse,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'.cluster/campaigns/dynamic-plan'
def read(path):
 try:return json.loads(path.read_text())
 except FileNotFoundError:return {}
def summarize(case):
 paths=sorted((ROOT/'runs'/case).glob('attempt-*'))
 if not paths:return {'case':case,'status':'not_run'}
 p=paths[-1];s=read(p/'state.json');a=read(p/'internal-final.json');v=read(p/'artifact-verification.json');c=read(p/'cleanup.json')
 usage=a.get('usage',[]);models=[x for x in usage if x['category']=='llm']
 return {'case':case,'attempt':p.name,'status':s.get('status'),'release':s.get('release',{}).get('sha256','')[:12],
  'outcome':s.get('outcome'),'error':s.get('error'),'seconds':round(s.get('finished_at',s.get('started_at',0))-s.get('started_at',0),1),
  'program_issues':a.get('program_issues'),'dependency_issues':a.get('dependency_issues'),'audit_complete':a.get('complete'),
  'artifact_passed':v.get('passed'),'artifact_errors':v.get('errors'),'artifact_checks':v.get('checks'),
  'browser':a.get('dimensions',{}).get('browser'),
  'workflows':[{'id':r['id'],'status':r['status'],'version':r['graph_version'],'sealed':r['sealed'],
                'tasks':[(t['id'],t['type'],t['status'],t['error_code']) for t in r['tasks']],
                'provider_calls':[(x['operation'],x['state']) for x in r.get('audit',{}).get('provider_calls',[])]} for r in a.get('database',{}).get('runs',[])],
  'model_calls':len(models),'model_tokens':sum(x['total_tokens'] or 0 for x in models),'missing_model_usage':sum(x['receipt']!='reported' for x in models),
  'diagnostics':[(x['step'],x['record']) for d in a.get('diagnostics',[]) for x in d.get('execution_diagnostics',[])],
  'log_errors':[x for d in a.get('diagnostics',[]) for x in d.get('facts',{}).get('latest_errors',[])],
  'boss_objects':a.get('dimensions',{}).get('boss',{}).get('total'),'redis_streams':a.get('dimensions',{}).get('cache',{}).get('streams'),
  'observer':a.get('dimensions',{}).get('observability'),'faults':s.get('faults'), 'cleanup':c,'directory':str(p)}
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('cases');args=parser.parse_args()
 for case in args.cases.split(','):print(json.dumps(summarize(case),ensure_ascii=False))
