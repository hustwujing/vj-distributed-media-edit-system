"""Rebuild the full manifest report from immutable attempt evidence; never infer a pass."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime,timezone,timedelta
try:
    from .campaign_dynamic_status import failure_classification,matches_definition
except ImportError:
    from campaign_dynamic_status import failure_classification,matches_definition

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'.cluster/campaigns/dynamic-plan'
DIMENSIONS=('browser','logs','observability','database','cache','boss','candidate_archive','local_cleanup','external_cleanup')
LABELS={'not_run':'未执行','running':'执行中','passed':'通过','program_failed':'程序缺陷','artifact_failed':'成果验收失败',
        'handled_failure':'模型/依赖失败已正确处理','blocked':'阻塞或证据不完整','review_required':'待逐例审查','obsolete':'需在当前版本复测'}

def read(path,default=None):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return {} if default is None else default

def timestamp(value):
    return datetime.fromtimestamp(value,timezone(timedelta(hours=8))).isoformat(timespec='seconds') if isinstance(value,(int,float)) else '未结束'

def attempt(directory,current,spec):
    state=read(directory/'state.json');internal=read(directory/'internal-final.json');cleanup=read(directory/'cleanup.json')
    artifact=read(directory/'artifact-verification.json');review=read(directory/'review.json')
    issues=list(internal.get('program_issues',[]))
    gaps=[name for name in DIMENSIONS if not internal.get('dimensions',{}).get(name,{}).get('checked')]
    if state.get('audit_error'):gaps.append('audit: '+state['audit_error'])
    if not cleanup.get('complete') and not (cleanup.get('expected_retention') and review.get('retention_verified')):gaps.append('cleanup')
    if not internal.get('complete'):gaps.append('audit incomplete')
    if state.get('restore_error'):issues.append('Fault restoration: '+state['restore_error'])
    # A human/agent review records what actually happened, including trigger coverage.
    reviewed=review.get('complete') and review.get('program_behavior_verified') and review.get('browser_verified') and review.get('required_boundary_verified')
    if state.get('status')!='finished':verdict='running'
    elif issues or review.get('outcome')=='program_failed':verdict='program_failed'
    elif review.get('outcome')=='artifact_failed' or artifact and not artifact.get('passed'):verdict='artifact_failed'
    elif gaps:verdict='blocked'
    elif not reviewed:verdict='review_required'
    elif review.get('outcome')=='handled_failure':verdict='handled_failure'
    elif state.get('outcome') in ('business_succeeded','protocol_passed'):
        verdict='passed' if (state.get('outcome')=='protocol_passed' or artifact.get('passed')) else 'blocked'
    else:verdict='blocked'
    version=state.get('release',{}).get('sha256')
    valid_current=bool(version and version==current.get('sha256') and matches_definition(state,spec,directory))
    evidence=[]
    for file in sorted(directory.rglob('*')):
        if file.is_file() and not any(part.startswith('private-') for part in file.parts) and file.name!='browser-state.json':
            with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            evidence.append({'path':str(file.relative_to(ROOT)),'bytes':file.stat().st_size,'sha256':digest})
    return {'attempt':directory.name,'path':str(directory),'verdict':verdict,'current_release':valid_current,
            'failure_classification':failure_classification(state,internal,review) if verdict in ('program_failed','artifact_failed','handled_failure','blocked') else None,
            'version':version,'started_at':state.get('started_at'),'finished_at':state.get('finished_at'),
            'outcome':state.get('outcome'),'error':state.get('error'),'issues':issues,'gaps':gaps,
            'run_ids':state.get('run_ids',[]),'project':state.get('project'),
            'events':[e for e in state.get('events',[]) if e.get('action')!='snapshot'],
            'base_url':state.get('base_url'),'authorizations':state.get('authorizations',[]),
            'faults':state.get('faults',[]),'error_codes':state.get('error_codes',[]),
            'dimensions':internal.get('dimensions',{}),'artifact_checks':artifact,'cleanup':cleanup,'review':review,
            'evidence':evidence}

def report():
    manifest=read(REPO/'docs/real-case-campaign/dynamic-cases.json');current=read(REPO/'.cluster/release.json')
    validation=read(Path(current['source'])/'docs/test-evidence/distributed-suite.json') if current.get('source') else {}
    rows=[]
    for spec in manifest['cases']:
        attempts=[attempt(directory,current,spec) for directory in sorted((ROOT/'runs'/spec['id']).glob('attempt-*'))]
        last=attempts[-1] if attempts else None
        verdict=last['verdict'] if last else 'not_run'
        if last and not last['current_release']:verdict='obsolete'
        rows.append({'id':spec['id'],'title':spec['title'],'required_boundary':spec.get('dynamic_hook'),
                     'verdict':verdict,'attempts':attempts,'definition':spec})
    counts=dict(Counter(row['verdict'] for row in rows))
    failure_counts=dict(Counter(row['attempts'][-1]['failure_classification'] for row in rows
        if row['attempts'] and row['attempts'][-1]['current_release'] and row['attempts'][-1]['failure_classification']))
    open_regressions=[item for item in read(ROOT/'open-regressions.json',[]) if item.get('status')!='resolved']
    result={'generated_at':time.time(),'release':current,'total':len(rows),'counts':counts,
            'failure_counts':failure_counts,
            'open_regressions':open_regressions,
            'complete':not open_regressions and all(row['verdict'] in ('passed','handled_failure') for row in rows),'validation':validation,'cases':rows}
    ROOT.mkdir(parents=True,exist_ok=True)
    (ROOT/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    lines=['# 动态工作流完整真机 case 测试报告','',
        f"本报告包含清单中的全部 {manifest['total_count']} 条 case 的执行状态和所有历史尝试。未执行、未触发目标边界、缺少审查证据以及旧版本结果均不计为当前版本通过。",'',
        f"当前部署：`{current.get('sha256','未部署')}`。整体验收：**{'完成' if result['complete'] else '未完成'}**。",'',
        f"冻结 Python 门禁：{validation.get('tests','未知')} 项，失败 {validation.get('failures','未知')}，错误 {validation.get('errors','未知')}，跳过 {validation.get('skipped','未知')}。绑定摘要：`{validation.get('source_sha256','无')}`。",
        'JavaScript 门禁与发布日志：`'+str(REPO/'.cluster/verification'/(Path(current.get('source','unknown')).name+'.log'))+'`。实际模型、外部服务和浏览器结果以下面的逐例证据为准。','',
        '统计：'+'；'.join(f'{LABELS[k]} {v}' for k,v in counts.items())+'。','',
        '当前版本失败分类：程序错误 '+str(failure_counts.get('program_error',0))+'；非程序错误（程序处理正确） '+str(failure_counts.get('non_program_error',0))+'；待归因 '+str(failure_counts.get('pending_classification',0))+'。非程序错误仍是业务或用例失败，不并入业务成功。','',
        '必须完成的额外回归：'+('；'.join(item['id']+'：'+item['reason'] for item in open_regressions) if open_regressions else '无未完成项')+'。','',
        '证据根目录：`'+str(ROOT)+'`；机器可读报告：`report.json`。原始证据保留在本机，不包含在 Git 提交中。','',
        '[用例保留与调整审查](dynamic-case-review.md) · [失败与修复记录](dynamic-fixes.md) · [非程序错误分析](dynamic-non-program-failures.md)','',
        '| Case | 场景 | 当前结论 | 尝试次数 |','| --- | --- | --- | --- |']
    batches={}
    for row in rows:
        for item in row['attempts']:
            batch=batches.setdefault(item['version'] or 'unknown',{'cases':set(),'attempts':0,'finished':0,'first':item['started_at'] or 0})
            batch['cases'].add(row['id']);batch['attempts']+=1;batch['finished']+=bool(item['finished_at']);batch['first']=min(batch['first'],item['started_at'] or 0)
    batch_lines=['',f"部署后真机批次按源码版本统计。每个版本可能包含定向验证、失败复测或部分全量运行，不能把候选版本数表述为完整{manifest['total_count']}条测试轮数。",'',
                 '| 部署版本 | 涉及用例数 | 实际执行次数 | 已结束次数 |','| --- | --- | --- | --- |']
    for version,batch in sorted(batches.items(),key=lambda item:item[1]['first']):
        batch_lines.append(f"| `{version[:12]}` | {len(batch['cases'])} | {batch['attempts']} | {batch['finished']} |")
    batch_lines.append('')
    lines[-2:-2]=batch_lines
    for row in rows:lines.append(f"| {row['id']} | {row['title']} | {LABELS[row['verdict']]} | {len(row['attempts'])} |")
    if validation.get('skipped_tests'):
        lines.extend(['','Python 门禁跳过项（不计作真实模型通过证据）：',''])
        lines.extend('- `'+item['test']+'`：'+item['reason'] for item in validation['skipped_tests'])
    for row in rows:
        lines.extend(['',f"## {row['id']} · {row['title']}",'',f"当前结论：**{LABELS[row['verdict']]}**。目标边界：`{row['required_boundary'] or '业务成果及全链路状态'}`。"])
        spec=row['definition']
        lines.extend(['','输入素材：'+('、'.join(spec['assets']) or '无')+'。',
                      '执行目标：'+spec['prompt'],
                      '独立成果预期：`'+json.dumps(spec['expect'],ensure_ascii=False)+'`。',
                      '外部调用范围：'+('、'.join(spec.get('allowed_external_operations',[])) or '未预先授权外部付费调用')+'。'])
        if not row['attempts']:lines.append('\n尚未执行，无通过证据。');continue
        for item in row['attempts']:
            directory=Path(item['path']);lines.extend(['',f"### {item['attempt']} · {LABELS[item['verdict']]}",'',
                f"版本：`{item['version']}`；是否当前部署：{'是' if item['current_release'] else '否'}。",
                f"时间（UTC+08:00）：{timestamp(item['started_at'])} → {timestamp(item['finished_at'])}。入口：`{item['base_url'] or '见当次驱动快照'}`。",
                f"执行结果：`{item['outcome']}`；项目：`{item['project']}`。",
                ' · '.join(f'[{label}]({directory/name})' for label,name in [('当次用例定义','case-definition.json'),('浏览器与执行记录','state.json'),('最终页面','final-browser.png'),('失败页面','failure-browser.png'),('成果页面','artifact-browser.png'),('逐例审查','review.json'),('内部审计','internal-final.json'),('取消后审计','internal-snapshot.json'),('候选文件索引','candidate-archive.json'),('运行日志证据','runtime-evidence.json'),('产物检查','artifact-verification.json'),('清理记录','cleanup.json')] if (directory/name).exists()),'',
                '| 维度 | 已取证 | 说明 |','| --- | --- | --- |'])
            for name in DIMENSIONS:
                dimension=item['dimensions'].get(name,{})
                detail=dimension.get('error_type') or dimension.get('coverage') or dimension.get('evidence') or dimension.get('index') or ''
                lines.append(f"| {name} | {'是' if dimension.get('checked') else '否'} | {detail} |")
            for label,value in [('执行错误',item['error']),('程序问题',item['issues']),('证据缺口',item['gaps']),('独立成果检查错误',item['artifact_checks'].get('errors')),('审查与修复记录',item['review'])]:
                if value:lines.extend(['',label+'：'+(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))])
            if item['failure_classification']:
                lines.extend(['','失败分类：'+{'program_error':'程序错误','non_program_error':'非程序错误（程序处理正确）','pending_classification':'待归因'}[item['failure_classification']]+'。'])
            if item['events']:lines.extend(['','实际控制步骤：'+ ' → '.join(e['action'] for e in item['events'])+'。'])
            if item['faults']:lines.extend(['','故障注入：'+ ' → '.join(e['action'] for e in item['faults'])+'。'])
            if item['artifact_checks'].get('checks'):lines.extend(['','独立成果检查值：`'+json.dumps(item['artifact_checks']['checks'],ensure_ascii=False)+'`。'])
            if item['authorizations']:
                lines.extend(['','实际确认的执行轮次：',''])
                for authorization in item['authorizations']:
                    lines.append('- `'+authorization['run_id']+'`：'+authorization['goal']+'；Token上限 '+str(authorization['model_token_budget'])+'；外部调用上限 `'+json.dumps(authorization['provider_call_budget'],ensure_ascii=False)+'`。')
    target=REPO/'docs/real-case-campaign/dynamic-test-report.md';target.write_text('\n'.join(lines)+'\n')
    print(json.dumps({'complete':result['complete'],'counts':counts,'report':str(target)},ensure_ascii=False))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--require-complete',action='store_true');args=parser.parse_args()
    result=report()
    if args.require_complete and not result['complete']:raise SystemExit(1)
