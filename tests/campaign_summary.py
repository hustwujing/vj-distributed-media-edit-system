"""Summarize actual attempts without turning unreviewed results into passes."""
from pathlib import Path
import json
import time
REPO=Path(__file__).resolve().parents[1];ROOT=REPO/'.cluster/campaigns/20261031-real60';DOC=REPO/'docs/real-case-campaign'
def main():
    manifest=json.loads((DOC/'cases.json').read_text());results=[]
    for case in manifest['cases']:
        runs=sorted((ROOT/'runs'/case['id']).glob('attempt-*/state.json'))
        attempts=[json.loads(p.read_text()) for p in runs]
        latest=attempts[-1] if attempts else None
        passed=bool(latest and (case.get('entry_mode') not in ('database_fixture_real_execution','browser_real_replan') or latest.get('cleanup',{}).get('passed')) and latest['status']=='succeeded' and latest.get('verification',{}).get('passed') and isinstance(latest.get('content_review'),dict) and latest['content_review'].get('passed'))
        status='passed' if passed else 'awaiting_content_review' if latest and latest['status']=='succeeded' else latest['status'] if latest else 'not_run'
        entry={'case':case['id'],'title':case['title'],'complexity':case['complexity'],'status':status,'attempts':len(attempts),
               'project':latest.get('project') if latest else None,'task':latest.get('execute_id') if latest else None,
               'elapsed_seconds':latest.get('elapsed_seconds') if latest else None,'release':(latest.get('execute_release') or latest.get('release')) if latest else None,
               'error':latest.get('error','').split('\n')[0] if latest else '', 'evidence':str(runs[-1].parent.relative_to(REPO)) if runs else None}
        if latest:
            directory=runs[-1].parent;turns=[]
            for path in [*(directory/'plan-history').glob('*.json'),directory/'execute.json']:
                if path.exists():turns.append(json.loads(path.read_text()))
            if not (directory/'plan-history').exists() and (directory/'plan.json').exists():turns.append(json.loads((directory/'plan.json').read_text()))
            entry['task_wall_seconds']=round(sum(max(0,t['updated']-t['created']) for t in turns),2)
            by_id={t['id']:t for t in turns};review=0
            for event in latest.get('events',[]):
                if event.get('action')=='plan_review':
                    decision=event['decision'];turn=by_id.get(decision['plan_id'])
                    if turn:review+=max(0,decision.get('at',turn['updated'])-turn['updated'])
            entry['tester_review_wait_seconds']=round(review,2)
        current_release=json.loads((REPO/'.cluster/release.json').read_text()) if (REPO/'.cluster/release.json').exists() else {}
        entry['validation_scope']='current_release' if passed and entry.get('release',{}).get('sha256')==current_release.get('sha256') else 'historical' if passed else 'not_passed'
        entry['entry_mode']=case.get('entry_mode','browser')
        entry['cleanup']=latest.get('cleanup') if latest else None
        results.append(entry)
    counts={status:sum(r['status']==status for r in results) for status in sorted({r['status'] for r in results})}
    report={'campaign':manifest['campaign'],'generated_at':time.time(),'counts':counts,'total':len(manifest['cases']),'results':results}
    (DOC/'progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=['# 真实场景测试进度','','这是进行中记录；只有实际产物验收和内容复核均通过，才标记通过。最终报告另行生成。','','| Case | 难度 | 场景 | 当前结果 | 尝试 | 端到端秒数 | 后端任务墙钟秒数 | 审查等待秒数 |','| --- | --- | --- | --- | --- | --- | --- | --- |']
    for r in results:lines.append(f"| {r['case']} | {r['complexity']} | {r['title']} | {r['status']} | {r['attempts']} | {r['elapsed_seconds'] or ''} | {r.get('task_wall_seconds','')} | {r.get('tester_review_wait_seconds','')} |")
    (DOC/'progress.md').write_text('\n'.join(lines)+'\n');print(json.dumps(counts))
if __name__=='__main__':main()
