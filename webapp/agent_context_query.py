"""Read public project history through the current task's fenced gateway."""
import argparse
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest='action', required=True)
    turns = actions.add_parser('turns');turns.add_argument('--before')
    message = actions.add_parser('message');message.add_argument('--task', required=True)
    message.add_argument('--field', choices=['prompt', 'text', 'error'], default='text')
    message.add_argument('--offset', type=int, default=0)
    tools = actions.add_parser('tools');tools.add_argument('--task', required=True)
    tools.add_argument('--after', type=int, default=0)
    project_data=actions.add_parser('project_data');project_data.add_argument('--kind',required=True,choices=['notes','chapters','branches','brand_templates']);project_data.add_argument('--offset',type=int,default=0)
    feedback=actions.add_parser('feedback');feedback.add_argument('--task',required=True);feedback.add_argument('--offset',type=int,default=0)
    budget=actions.add_parser('budget');budget.add_argument('--task',required=True)
    body = {k: v for k, v in vars(parser.parse_args()).items() if v is not None}
    request = Request('http://127.0.0.1:18766/context', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
    except HTTPError as error:
        raise SystemExit(error.read().decode())


if __name__ == '__main__':main()
