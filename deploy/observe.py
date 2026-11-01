"""Root-only diagnostics for the installed release, never mutable workspace code."""
import argparse
import json
import os
from pathlib import Path
import sys

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from deploy import cluster


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['task','request','logs','status','alerts','metrics','maintenance'])
    parser.add_argument('identifier',nargs='?');parser.add_argument('--after',type=int,default=0);parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--full',action='store_true',help='Expand complete bounded evidence')
    parser.add_argument('--output',type=Path);args=parser.parse_args(argv)
    if os.geteuid()!=0:raise SystemExit('Operator diagnostics require root.')
    info=json.loads((cluster.ROOT/'release.json').read_text());sys.path.insert(0,info['source'])
    os.environ.update(cluster.environment())
    os.environ.update(cluster.env_file(cluster.CONF/'observation.env'))
    from webapp.diagnostics import diagnose,resolve_request,observation
    from webapp.store import Store
    store=Store(cluster.data('node','a'),initialize_schema=False,timeout=1)
    if args.action=='task':value=diagnose(store,args.identifier or '',args.after,args.limit)
    elif args.action=='request':
        try:tasks=resolve_request(store,args.identifier or '')
        except Exception:tasks=[]
        value={'task_ids':tasks,'logs':observation('/logs',{'request_id':args.identifier or '', 'limit':args.limit,'after':args.after})}
    elif args.action=='logs':value=observation('/logs',{'task_id':args.identifier or '', 'limit':args.limit,'after':args.after})
    elif args.action=='maintenance':
        path=cluster.ROOT/'observability/maintenance'
        if args.identifier=='on':path.touch(mode=0o600)
        elif args.identifier=='off':path.unlink(missing_ok=True)
        else:raise SystemExit('Specify on or off')
        value={'maintenance':path.exists()}
    elif args.action=='metrics':value=observation('/snapshot')
    else:value=observation('/health' if args.action=='status' else '/alerts')
    if args.action=='task' and not args.full and not args.output:
        value={k:value[k] for k in ('task_id','task','facts','suggestions','complete','gaps','evidence_scope')}
        value['details']='Use --full or --output to expand the bounded evidence bundle.'
    body=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        descriptor=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as stream:stream.write(body)
        print('Saved diagnostic bundle:',args.output)
    else:print(body)


if __name__=='__main__':main()
