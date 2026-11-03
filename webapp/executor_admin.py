"""Local administrator CLI. Uses the same database as the API/executors."""
import argparse
import json
from .store import Store
from .paths import data_root
from .task_leases import initialize, CLOCK


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    commands.add_parser('status')
    drain=commands.add_parser('drain');drain.add_argument('worker')
    reconcile=commands.add_parser('reconcile');reconcile.add_argument('call_id')
    garbage=commands.add_parser('gc');garbage.add_argument('--limit',type=int,default=100)
    args=parser.parse_args()
    store=Store(data_root());initialize(store)
    if args.action=='drain':
        changed=store.execute('UPDATE execution_workers SET draining=1 WHERE id=?',(args.worker,))
        if not changed:raise SystemExit('Unknown executor instance')
        print('Drain requested; existing tasks continue until completion or the drain deadline.')
    elif args.action=='reconcile':
        from .external_cleanup import reconcile
        reconcile(store,args.call_id)
        print('Bounded upstream status reconciliation queued; business execution stays stopped.')
    elif args.action=='gc':
        if not 1<=args.limit<=1000:raise SystemExit('GC limit must be 1–1000')
        print(json.dumps({'collected':store.artifacts.collect_garbage(args.limit)}))
    else:
        workers=store.query('SELECT *,heartbeat>'+CLOCK+'-20 AS healthy FROM execution_workers ORDER BY heartbeat DESC LIMIT 100')
        active=store.query('SELECT run_id,worker,attempt,generation,kind,expires,expires<='+CLOCK+' AS expired FROM execution_leases WHERE active=1')
        cleanup=store.query("SELECT attempt,task_id,node,state,retries,error FROM execution_cleanup WHERE state<>'complete' ORDER BY updated LIMIT 100")
        limits=store.query("SELECT * FROM execution_limits WHERE termination_reason<>'' ORDER BY deadline_at DESC LIMIT 100")
        external=store.query("SELECT id,task_id,state,attempts,error FROM execution_external_cleanup WHERE state<>'complete' LIMIT 100")
        print(json.dumps({'workers':workers,'active':active,'cleanup':cleanup,'terminated':limits,'external_cleanup':external},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
