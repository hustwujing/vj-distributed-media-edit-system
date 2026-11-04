"""Report paired proposal trials without conflating media success or provider failures."""
import argparse,collections,csv,hashlib,json,math,statistics,time,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.campaign_model_fixture import facts_digest
MODELS=['gpt-5.6','claude-4.6-sonnet','deepseek-v4.1-flash']
ARMS={'baseline':'原动态v2文本协议','legacy_strict':'完整旧契约＋原生strict','commands_plain':'精简命令＋非strict','commands_strict':'精简命令＋strict优先／显式回退','commands_strict_generic_feedback':'精简命令＋strict优先＋泛化反馈',
      'draft_strict':'PlanDraft类型化命令＋strict优先／显式回退'}

def percentile(values,p):
    return round(sorted(values)[max(0,math.ceil(len(values)*p)-1)],2) if values else None

def ratio(n,d):return f'{n}/{d} ({n/d:.1%})' if d else '—'

def outcome(row):
    if row.get('user_stopped'):return 'user_stopped'
    if row.get('unscorable'):return 'unscorable'
    if row['contract_valid']:return 'valid'
    if row.get('dependency_error'):return 'dependency_error'
    last=row['calls'][-1] if row['calls'] else {}
    if any(e.get('message')=='Finalize requires proposal' for e in last.get('errors',[])):
        return 'syntax_or_envelope_error'
    if not last.get('syntax_valid'):return 'syntax_or_envelope_error'
    if not last.get('schema_valid'):return 'schema_or_compile_error'
    return 'planning_contract_error'

def summarize(rows):
    calls=[call for row in rows for call in row['calls']]
    received=[row for row in rows if any(call.get('http_status')==200 for call in row['calls'])]
    used=[call.get('usage_normalized') for call in calls if call.get('usage_normalized')]
    return {'finished':len(rows),'outcomes':dict(collections.Counter(outcome(r) for r in rows)),
            'valid_result_kinds':dict(collections.Counter(r['kind'] for r in rows if r['contract_valid'])),
            'first_contract_valid':sum(r['first_contract_valid'] for r in rows),
            'first_schema_valid':sum(r['first_schema_valid'] for r in rows),'received_reply_trials':len(received),
            'contract_valid':sum(r['contract_valid'] for r in rows),'dependency_errors':sum(r.get('dependency_error',False) for r in rows),
            'dependency_error_types':dict(collections.Counter(c.get('error_type','unknown') for c in calls if c.get('dependency_error') and not c.get('fallback'))),
            'unscorable':sum(r.get('unscorable',False) for r in rows),'model_calls':len(calls),
            'mean_seconds':round(statistics.mean(r['seconds'] for r in rows),2) if rows else None,
            'valid_mean_seconds':round(statistics.mean(r['seconds'] for r in rows if r['contract_valid']),2) if any(r['contract_valid'] for r in rows) else None,
            'median_seconds':round(statistics.median(r['seconds'] for r in rows),2) if rows else None,
            'p95_seconds':percentile([r['seconds'] for r in rows],.95),
            'strict_fallback_calls':sum(bool(c.get('fallback')) for c in calls),
            'strict_wire_violations':sum(bool(c.get('strict_constraint_violation')) for c in calls),
            'usage_missing_calls':sum(not c.get('usage_normalized') for c in calls),
            'output_usage_suspect_calls':sum(c.get('output_usage_suspect',False) for c in calls),
            'reported_input_tokens':sum(u.get('input_tokens') or 0 for u in used),
            'reported_output_tokens':sum(u.get('output_tokens') or 0 for u in used),
            'reported_total_tokens':sum(u['total_tokens'] for u in used if u.get('total_tokens') is not None),
            'total_token_receipt_calls':sum(u.get('total_tokens') is not None for u in used)}

def report(directory,ablation_directory,models=MODELS,report_stem='model-reply-correctness'):
    if len(models)!=3 or len(set(models))!=3:raise ValueError('Exactly three distinct model identifiers required')
    if not report_stem or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in report_stem):raise ValueError('Invalid report filename stem')
    dataset=json.loads((directory/'dataset.json').read_text());source=json.loads((directory/'source.json').read_text())
    cases=dataset['cases'];primary=[];extra=[];ablation_source=None
    for p in (directory/'results').glob('*/*/*/*/result.json'):
        r=json.loads(p.read_text())
        if r['variant'] in ('baseline','commands_strict'):primary.append(r)
    if (ablation_directory/'dataset.json').exists():
        ablation_dataset=json.loads((ablation_directory/'dataset.json').read_text())
        if facts_digest(dataset)!=facts_digest(ablation_dataset):raise ValueError('Ablation facts or scope differ from primary')
        if (directory/'baseline-instructions.txt').read_bytes()!=(ablation_directory/'baseline-instructions.txt').read_bytes():
            raise ValueError('Ablation baseline instructions differ from primary')
        ablation_source=json.loads((ablation_directory/'source.json').read_text())
        extra=[json.loads(p.read_text()) for p in (ablation_directory/'results').glob('*/*/*/*/result.json')]
    recorded=primary+extra
    stopped_rows=[r for r in recorded if r.get('user_stopped')]
    primary=[r for r in primary if not r.get('user_stopped')]
    extra=[r for r in extra if not r.get('user_stopped')]
    rows=primary+extra
    stop_info=json.loads((directory/'stopped-by-user.json').read_text()) if (directory/'stopped-by-user.json').exists() else None
    primary_expected=len(cases)*len(models)*2*3;development=[c for c in cases if c['split']=='development']
    expected=primary_expected+len(development)*len(models)*3*3
    issues=[]
    for root in (directory,ablation_directory):
        if (root/'evaluator-errors.json').exists():issues.extend(json.loads((root/'evaluator-errors.json').read_text()))
    expected_keys={(c['id'],m,a,n) for c in cases for m in models for a in ('baseline','commands_strict') for n in range(3)}
    expected_keys|={(c['id'],m,a,n) for c in development for m in models for a in ('legacy_strict','commands_plain','commands_strict_generic_feedback') for n in range(3)}
    keys=[(r['case'],r['model'],r['variant'],r['repeat']) for r in recorded]
    if len(keys)!=len(set(keys)) or set(keys)-expected_keys:raise ValueError('Duplicate or unexpected evaluation trial')
    complete=set(keys)==expected_keys and not issues and not any(r.get('unscorable') or r.get('user_stopped') for r in recorded)
    grouped=[]
    for model in models:
        for arm in ARMS:
            subset=[r for r in rows if r['model']==model and r['variant']==arm]
            grouped.append({'model':model,'variant':arm,**summarize(subset)})
    paired=[]
    for model in models:
        by={(r['case'],r['repeat'],r['variant']):r for r in primary if r['model']==model};wins=losses=ties=excluded=0
        for case in cases:
            for repeat in range(3):
                old=by.get((case['id'],repeat,'baseline'));new=by.get((case['id'],repeat,'commands_strict'))
                if not old or not new or old.get('dependency_error') or new.get('dependency_error'):excluded+=1;continue
                a,b=old['contract_valid'],new['contract_valid']
                if a==b:ties+=1
                elif b:wins+=1
                else:losses+=1
        paired.append({'model':model,'new_only_valid':wins,'old_only_valid':losses,'same_outcome':ties,'incomplete_or_dependency_excluded':excluded})
    result={'generated_at':time.time(),'complete':complete,'expected_trials':expected,'finished_trials':len(rows),
            'recorded_trials':len(recorded),'user_stopped_trials':len(stopped_rows),'stop_info':stop_info,
            'source':source,'ablation_source':ablation_source,'ablation_directory':str(ablation_directory),
            'dataset_sha256':hashlib.sha256((directory/'dataset.json').read_bytes()).hexdigest(),'facts_sha256':facts_digest(dataset),
            'groups':grouped,'paired':paired,'summary':summarize(rows),'evaluator_errors':issues,
            'business_actions_executed':sum(r.get('business_actions_executed',0) for r in rows)}
    (directory/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    protocol_note=('主比较完成后发现Responses省略strict字段仍可能自动启用严格模式，修正为显式strict=false后另冻结拆分实验。提前启动及策略摘要绑定未更新的拆分请求均单独归档，不进入本报告。GPT主比较没有触发strict回退，其他两种模型的传输行为不变，主比较保留。'
                   if source['sha256'].startswith('705dcb9a9211') else
                   '本轮Responses非strict请求显式设置strict=false。模型、协议、high配置、重复次数和超时在启动时冻结，不在进行中的试次间切换。评测脚本和应用源码分别保存来源摘要。')
    lines=['# 模型回复协议改造与三模型对照报告','',f"更新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}。状态：{'已完成' if complete else '用户停止，属于部分结果' if stop_info else '执行中，不能作为最终结论'}。",'',
        f"已结束 **{len(rows)}/{expected}** 组；主比较 {len(primary)}/{primary_expected} 组。应用源码 `{source['sha256']}`。",'',
        f"用户中止{len(stopped_rows)}次，另列并排除协议合规率及平均耗时统计。"+(f"未启动{stop_info['not_started']}次，后续拆分实验未执行。" if stop_info else ''),'',
        '本报告评估冻结规划请求的结构和业务契约合法性，不等于方案一定满足全部内容目标或媒体产物成功率，也不计入119条真机通过数。旧基线是本轮改造前的动态structured-profile-v2文本协议，不是main分支的非动态SDK版。','',
        '## 方法和边界','',
        f"30个真实来源场景：首次规划10、后续规划9、失败修复6、用户反馈4、重新授权1。开发{len(development)}、保留{len(cases)-len(development)}；已暴露试运行请求均分入开发集。各模型各场景重复3次，每次最多两次模型纠正。",'',
        '开发/保留按冻结请求划分，部分请求来自同一业务case，保留集不等于全新业务类别。旧协议组使用原v2提示和文本封装，所有组共用冻结的当前业务校验器及有界纠错；这不是旧服务与新服务整套执行链的比较。', '',
        '所有组使用相同冻结事实和完整相关Operation契约，固定各自模型high配置。不同厂商的high不代表相同算力。仅生成提案并在只读内存状态上验证，无媒体、Provider业务操作或正式Run提交。缺失的持久化计数规范化为0，因此不声称覆盖事务竞争和真实修复预算边界。', '',
        '先前缺Operation完整契约的试运行结果仅用于开发排障，不混入本表。strict字段被网关接受不等于约束生效；发生明确拒绝或实际传输Schema违规时记录回退，本地Schema、授权和业务验证继续执行。', '',
        protocol_note, '',
        '拆分实验源码：`'+(ablation_source['sha256'] if ablation_source else '尚未冻结')+'`；仅显式重绑定评测代码的策略摘要及其snapshot_digest，目标、素材、授权范围、验收、历史等事实的规范化摘要与主比较一致，启动前校验策略与快照绑定。', '',
        '## 主比较','',
        '首次合法率的分母为收到HTTP 200模型响应的试次；端到端最终合法率的分母为所有已结束试次，包含模型调用异常。模型调用异常单列，包含连接失败、读取超时或上游HTTP错误，不能解释为模型纠正失败。', '',
        '平均耗时按试次中的生成、校验和纠错总耗时计算，不含排队、准备上下文、媒体执行或正式预算计数接口。全体平均包含超时，合规平均只统计最终合法试次。两种分母分别报告，避免选择性排除失败掩盖等待成本。', '',
        '| 模型 | 协议 | 已结束/90 | 首次契约合法 | 最终合法/全部结束 | 模型调用异常 | 全体平均/合规平均秒 | 中位/P95秒 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for g in grouped:
        if g['variant'] not in ('baseline','commands_strict'):continue
        lines.append(f"| {g['model']} | {ARMS[g['variant']]} | {g['finished']}/90 | {ratio(g['first_contract_valid'],g['received_reply_trials'])} | {ratio(g['contract_valid'],g['finished'])} | {g['dependency_errors']} | {g['mean_seconds']} / {g['valid_mean_seconds']} | {g['median_seconds']} / {g['p95_seconds']} |")
    lines+=['','失败按最终停止位置互斥分类；这是校验层级，不直接等同根因归属。模型调用异常优先单列，之前发生的模型错误仍保留在逐次回执中。','',
        '| 模型 | 协议 | 合法 | JSON/调用封装未通过 | 字段Schema/编译未通过 | 规划契约未通过 | 外部模型调用异常 | 待判定/用户停止 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for g in grouped:
        if g['variant'] not in ('baseline','commands_strict'):continue
        o=g['outcomes']
        lines.append(f"| {g['model']} | {ARMS[g['variant']]} | {o.get('valid',0)} | {o.get('syntax_or_envelope_error',0)} | {o.get('schema_or_compile_error',0)} | {o.get('planning_contract_error',0)} | {o.get('dependency_error',0)} | {o.get('unscorable',0)+o.get('user_stopped',0)} |")
    lines+=['','配对比较排除任一侧模型调用异常或尚未结束的试次：','', '| 模型 | 仅新协议合法 | 仅旧协议合法 | 结果相同 | 排除/未结束 |','| --- | ---: | ---: | ---: | ---: |']
    for p in paired:lines.append(f"| {p['model']} | {p['new_only_valid']} | {p['old_only_valid']} | {p['same_outcome']} | {p['incomplete_or_dependency_excluded']} |")
    lines+=['','合法结果类型分布用于识别合法但仅请求用户输入的回复，不能用保守停下来冒充完成任务：','',
            '| 模型 | 协议 | 合法结果类型与数量 |','| --- | --- | --- |']
    for model in models:
        for arm in ARMS:
            kinds=collections.Counter(r['kind'] for r in rows if r['model']==model and r['variant']==arm and r['contract_valid'])
            lines.append(f"| {model} | {ARMS[arm]} | "+', '.join(f'{k}: {v}' for k,v in sorted(kinds.items()))+' |')
    lines+=['','## 保留场景','', '| 模型 | 协议 | 已结束 | 首次合法 | 最终合法 | 模型调用异常 |','| --- | --- | ---: | ---: | ---: | ---: |']
    for model in models:
        for arm in ('baseline','commands_strict'):
            g=summarize([r for r in primary if r['model']==model and r['variant']==arm and r['split']=='held_out'])
            lines.append(f"| {model} | {ARMS[arm]} | {g['finished']} | {ratio(g['first_contract_valid'],g['received_reply_trials'])} | {ratio(g['contract_valid'],g['finished'])} | {g['dependency_errors']} |")
    lines+=['','## 开发样本拆分实验','', '| 模型 | 协议组合 | 已结束 | 首次合法 | 最终合法 | 模型调用异常 | 全体平均/合规平均秒 |','| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for model in models:
        for arm in ARMS:
            g=summarize([r for r in rows if r['model']==model and r['variant']==arm and r['split']=='development'])
            lines.append(f"| {model} | {ARMS[arm]} | {g['finished']} | {ratio(g['first_contract_valid'],g['received_reply_trials'])} | {ratio(g['contract_valid'],g['finished'])} | {g['dependency_errors']} | {g['mean_seconds']} / {g['valid_mean_seconds']} |")
    lines+=['','## 同批次首次调用的strict对照','',
        'commands_plain与commands_strict_generic_feedback在首次调用时使用相同提示、事实和命令Schema，仅strict请求策略不同。后续纠错反馈不同，因此两组最终合规率不能单独归因于strict开关。其他主比较与拆分组分时运行，存在网关负载、缓存和采样波动，不能把耗时差全部解释成协议因果。', '',
        '| 模型 | 首次调用策略 | 已结束试次 | 首次HTTP 200 | 首次调用即合法 | 首次调用异常 | 首次调用平均秒 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for model in models:
        for arm in ('commands_plain','commands_strict_generic_feedback'):
            subset=[r for r in extra if r['model']==model and r['variant']==arm]
            first=[r['calls'][0] for r in subset if r['calls']]
            avg=round(statistics.mean(c['seconds'] for c in first),2) if first else None
            lines.append(f"| {model} | {'非strict' if arm=='commands_plain' else 'strict优先'} | {len(subset)} | {sum(c.get('http_status')==200 for c in first)} | {sum(bool(c.get('contract_valid')) for c in first)} | {sum(bool(c.get('dependency_error')) for c in first)} | {avg} |")
    lines+=['','## 用量和strict实效','',
        '保留原始与规范化用量回执。缺失total不填0、不伪造上游total；可疑的output_tokens=0但有实际输出不用于宣称成本优势。不同协议的缓存计数及缺失字段需单独核对。', '',
        '| 模型 | 协议 | HTTP调用 | strict回退 | strict输出违规 | 缺用量回执 | 可疑零输出回执 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for g in grouped:lines.append(f"| {g['model']} | {ARMS[g['variant']]} | {g['model_calls']} | {g['strict_fallback_calls']} | {g['strict_wire_violations']} | {g['usage_missing_calls']} | {g['output_usage_suspect_calls']} |")
    lines+=['','## 接入与验证状态','',
        '- 原生工具可用性以本轮真实回执及提案校验为依据；DS4.1网关使用auto工具选择，先前强制选择探测被拒绝。',
        '- 先前Claude-4.6/DS4.1的messages/count_tokens接口返回404；新模型接入应另行探测，直接生成对照结果不能代替生产预算链路接入验证。',
        '- 发布门禁与媒体真机结果另行记录，历史版本通过不冒充当前验收。',
        '- 每组原始公开回复、逐次调用意图/回执、校验错误和冻结来源均保存在私有评测目录；不保存私有推理，也不发布凭据。', '',
        '逐组结果见同目录CSV；完整私有证据：`'+str(directory)+'`。']
    target=ROOT/'docs'/f'{report_stem}-report.md';target.write_text('\n'.join(lines)+'\n')
    csv_path=ROOT/'docs'/f'{report_stem}-trials.csv';fields=['case','source_case','split','model','variant','repeat','kind','first_syntax_valid','first_schema_valid','first_contract_valid','contract_valid','dependency_error','unscorable','user_stopped','seconds']
    with csv_path.open('w',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
        for r in sorted(recorded,key=lambda x:(x['case'],x['model'],x['variant'],x['repeat'])):writer.writerow({k:r.get(k) for k in fields})
    print(json.dumps({'complete':complete,'finished':len(rows),'expected':expected,'valid':result['summary']['contract_valid'],'report':str(target)}))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,default=ROOT/'.cluster/model-evals/reply-final-20261104');p.add_argument('--ablation-directory',type=Path,default=ROOT/'.cluster/model-evals/reply-ablations-bound-20261104');p.add_argument('--models',default=','.join(MODELS));p.add_argument('--report-stem',default='model-reply-correctness');p.add_argument('--require-complete',action='store_true');args=p.parse_args();value=report(args.directory,args.ablation_directory,args.models.split(','),args.report_stem)
    if args.require_complete and not value['complete']:raise SystemExit(1)
