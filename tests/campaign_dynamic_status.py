"""Count distinct cases on the deployed version, keeping attempts separate."""
import json
import hashlib
from functools import lru_cache
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'.cluster/campaigns/dynamic-plan'
def read(path):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return {}
@lru_cache(maxsize=32)
def source_hash(path):
    try:return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:return None

def matches_definition(state,spec,directory):
    if read(directory/'case-definition.json')!=spec:return False
    if state.get('driver_sha256')!=source_hash(REPO/'tests/campaign_dynamic_browser.cjs'):return False
    helpers=state.get('helper_sha256',{})
    return bool(helpers) and all(value==source_hash(REPO/'tests'/name) for name,value in helpers.items())
def failure_classification(state,internal,review):
    if review.get('outcome')=='program_failed' or internal.get('program_issues') or state.get('restore_error'):
        return 'program_error'
    if (all(review.get(k) for k in ('complete','program_behavior_verified','browser_verified','required_boundary_verified'))
            and internal.get('complete') and internal.get('program_passed')
            and not any(state.get(k) for k in ('audit_error','cleanup_error','restore_error'))):
        return 'non_program_error'
    return 'pending_classification'
def status():
    release=read(REPO/'.cluster/release.json');groups=defaultdict(list)
    for case in read(REPO/'docs/real-case-campaign/dynamic-cases.json')['cases']:
        attempts=sorted((ROOT/'runs'/case['id']).glob('attempt-*/state.json'))
        source=attempts[-1] if attempts else None;state=read(source) if source else {}
        if state.get('release',{}).get('sha256')!=release['sha256'] or not source:
            groups['not_run'].append(case['id']);continue
        if not matches_definition(state,case,source.parent) and read(source.parent/'review.json').get('outcome')!='program_failed':
            groups['not_run'].append(case['id']);continue
        if state.get('status')!='finished':groups['running'].append(case['id']);continue
        directory=source.parent;review=read(directory/'review.json');artifact=read(directory/'artifact-verification.json');internal=read(directory/'internal-final.json');cleanup=read(directory/'cleanup.json')
        passed=(state.get('outcome') in ('business_succeeded','protocol_passed') and internal.get('complete') and internal.get('program_passed') and artifact.get('passed',True) and not any(state.get(k) for k in ('audit_error','cleanup_error','restore_error')) and (cleanup.get('complete') or cleanup.get('expected_retention')) and review.get('outcome') not in ('program_failed','artifact_failed','handled_failure'))
        if passed:
            groups['passed'].append(case['id'])
            reviewed=all(review.get(k) for k in ('complete','program_behavior_verified','browser_verified','required_boundary_verified'))
            groups['reviewed_pass' if reviewed else 'pending_review'].append(case['id'])
        else:
            classification=failure_classification(state,internal,review)
            groups[classification].append(case['id'])
            groups['failed'].append({'case':case['id'],'outcome':state.get('outcome'),'error':state.get('error'),
                                    'review':review.get('failure_origin'),'classification':classification})
    keys=('passed','failed','running','not_run','reviewed_pass','pending_review')
    return {'at':datetime.now().astimezone().isoformat(timespec='seconds'),'release':release['sha256'][:12],
            'counts':{k:len(groups[k]) for k in keys},
            'failure_counts':{k:len(groups[k]) for k in ('program_error','non_program_error','pending_classification')},
            'running':groups['running'],'failures':groups['failed'],'pending_review':groups['pending_review']}
if __name__=='__main__':print(json.dumps(status(),ensure_ascii=False))
