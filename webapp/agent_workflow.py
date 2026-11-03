"""Agent client: persist a plan, then end the turn while the scheduler works."""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit', 'status','validate','decision','capabilities','intent','preflight'])
    parser.add_argument('plan', nargs='?')
    parser.add_argument('--operation',action='append',dest='operations',help='Filter capability schemas by operation ID (repeatable)')
    parser.add_argument('--step',action='append',dest='steps',help='Read full details of selected workflow steps (repeatable)')
    parser.add_argument('--full',action='store_true',help='Return all operation schemas instead of the discovery index')
    args = parser.parse_args()
    if args.full and (args.action!='capabilities' or args.operations):parser.error('--full requires unfiltered capabilities')
    if args.operations and args.action!='capabilities':parser.error('--operation requires capabilities')
    if args.steps and args.action!='status':parser.error('--step requires status')
    if args.action in ('submit','validate','decision','intent','preflight') and not args.plan:
        parser.error('This action requires a JSON file')
    body = {'action':args.action}
    if args.action=='capabilities' and not args.operations:body['compact']=not args.full
    if args.operations:body['operations']=args.operations
    if args.steps:body['steps']=args.steps
    if args.action in ('submit','validate','decision','intent','preflight'):
        body[args.action if args.action in ('decision','intent') else 'plan'] = json.loads(Path(args.plan).read_text())
    request = Request('http://127.0.0.1:18766/workflow', data=json.dumps(body).encode(),
                      headers={'Content-Type':'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
    except HTTPError as error:
        raise SystemExit(error.read().decode())


if __name__ == '__main__':
    main()
