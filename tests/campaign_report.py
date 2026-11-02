"""Generate auditable real-case report; pending/failed cases cannot become passes."""
import json
import hashlib
from pathlib import Path
import time
from campaign_summary import main as summarize
REPO=Path(__file__).resolve().parents[1]
DOC=REPO/'docs/real-case-campaign'
ROOT=REPO/'.cluster/campaigns/20261031-real60'

def main():
    summarize()
    progress=json.loads((DOC/'progress.json').read_text())
    manifest=json.loads((DOC/'cases.json').read_text())
    fingerprint=json.loads((DOC/'design-fingerprint.json').read_text())
    immutable=[{key:case[key] for key in fingerprint['immutable_fields']} for case in manifest['cases'] if case['id'][0] in ('S','C')]
    actual_digest=hashlib.sha256(json.dumps(immutable,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if len(immutable)!=60 or actual_digest!=fingerprint['sha256']:raise ValueError('Original case requirements changed; cannot produce a final report')

    from campaign_interventions import specification
    specification()
    bugs=json.loads((DOC/'bugs.json').read_text())
    total=len(manifest['cases'])
    complete=progress['counts'].get('passed')==total
    details=[];providers=[]
    for entry in progress['results']:
        record=dict(entry);attempts=[]
        for path in sorted((ROOT/'runs'/entry['case']).glob('attempt-*/state.json')):
            state=json.loads(path.read_text());directory=path.parent
            attempt={k:state.get(k) for k in ('status','started_at','finished_at','elapsed_seconds','plan_id','execute_id','release','plan_release','execute_release','error','content_review','page_errors','http_errors','network_errors')}
            attempt['evidence']=str(directory.relative_to(REPO));attempt['attempt']=directory.name
            attempt['review_and_resume_history']=[e for e in state.get('events',[]) if e.get('action') in ('plan_review','observer_resumed','user_stopped_for_replan')]
            attempt['plan_revisions']=len(list((directory/'plan-history').glob('*.json')))
            archive_index=directory/'trace-archive/index.json'
            if archive_index.exists():attempt['trace_archive']=json.loads(archive_index.read_text())
            verified=state.get('verification',{})
            attempt['validation']={k:verified.get(k) for k in ('passed','errors','checks','tooling')}
            attempt['artifacts']=[{k:a.get(k) for k in ('name','sha256','bytes','media','image','decoded_frames','eligible_delivery')} for a in verified.get('artifacts',[])]
            attempts.append(attempt)
            diagnosis=directory/'execute_id-final-diagnosis.json'
            if diagnosis.exists():
                d=json.loads(diagnosis.read_text())
                attempt['timing']=d.get('qa_timing')
                attempt['step_attempts']=d.get('steps')
                attempt['quality_events']=d.get('quality')
                for call in d.get('provider_calls',[]):providers.append({'case':entry['case'],'attempt':directory.name,**call})
        record['history']=attempts
        if record['status']=='passed':
            latest=ROOT/'runs'/entry['case']/attempts[-1]['attempt']
            current=json.loads((latest/'state.json').read_text())
            proof=latest/'primary-regression.json'
            name=json.loads(proof.read_text())['name'] if proof.exists() else None
            primary=next((a for a in current.get('artifacts',[]) if a['name']==name),None) if name else next((a for a in current.get('artifacts',[]) if a.get('is_primary')),None)
            if not primary:primary=next((a for a in current.get('artifacts',[]) if a['name']==current.get('primary_title')),None)
            if primary:
                verified=next((a for a in current.get('verification',{}).get('artifacts',[]) if a.get('local_file')==primary.get('local_file')),{})
                record['primary_artifact']={'name':Path(primary['name']).name,'path':primary['local_file'],'sha256':verified.get('sha256')}
        record['had_repair_or_replan']=len(attempts)>1 or any(a['plan_revisions']>1 or any(q.get('action')=='repair' for q in a.get('quality_events',[]) or []) for a in attempts)
        details.append(record)
    report={'complete':complete,'generated_at':time.time(),'counts':progress['counts'],'total':total,'cases':details,'bugs':bugs,'provider_calls':providers,'load_rules':manifest['rules'],
        'integrity':'Original 60 and R17 use real browser submissions; R01-R16 seed planning fixtures with user authorization. All outputs and reuse are produced by real deployed executors. Failed attempts retained.',
        'timing_scope':'End-to-end includes user-review and queue waits. Agent non-tool estimate includes model and orchestration, not pure inference. Audio checks use waveform measurements and browser media loading, not subjective listening.'}
    suffix='final-report' if complete else 'running-report'
    (DOC/(suffix+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines=[f'# {total}条真实场景测试'+('最终报告' if complete else '过程报告（尚未全部完成）'),'','完成状态：'+(f'{total} / {total} 条已有验收证据；历史版本与当前版本范围见 JSON validation_scope。' if complete else f"已通过 {progress['counts'].get('passed',0)} / {total}，其他状态见下表；不能据此宣称全量通过。"),'',
           '原始场景通过真实浏览器上传、输入、确认；R01–R16 按用户授权插库构造规划夹具并执行真实干预，R17 通过真实浏览器、Jev 与 Agent 重新规划，均运行部署后的服务，下载实际产物；保留失败尝试、用户反馈、步骤修订、数据库诊断及日志关联。素材、原始需求和验收项见 [设计](design.md) 与 [用例清单](cases.json)。','',
           '| Case | 场景 | 最终/当前状态 | 尝试次数 | 修复/改方案 | 端到端秒数 | 主交付 |','| --- | --- | --- | --- | --- | --- | --- |']
    for r in details:
        primary=r.get('primary_artifact');link=f"[{primary['name']}](<{primary['path']}>)" if primary else '—'
        lines.append(f"| {r['case']} | {r['title']} | {r['status']} | {r['attempts']} | {'是' if r['had_repair_or_replan'] else '否'} | {r.get('elapsed_seconds') or ''} | {link} |")
    lines+=['','## 逐条证据与尝试历史','']
    for r in details:
        lines+=['### '+r['case']+' '+r['title'],'']
        if r.get('task'):lines+=['最终/当前执行任务：`'+r['task']+'`。','']
        for a in r['history']:
            lines+=['- '+a['attempt']+'：'+str(a['status'])+'；证据目录 `'+a['evidence']+'`。']
            if a.get('error'):lines+=['- 该尝试问题：'+a['error'].split('\n')[0]]
            review=a.get('content_review')
            if isinstance(review,dict):lines+=['- 内容复核：'+str(review.get('note') or review.get('method'))]
            outputs=[x['name'] for x in a['artifacts'] if x.get('eligible_delivery')]
            if outputs:lines+=['- 实际下载交付：'+', '.join(outputs)]
        lines+=['']
    lines+=['## 统计边界','',
        '端到端耗时包含方案审阅和排队；后台任务墙钟时间与测试者等待分别记录。模型非工具耗时估算还包括编排/准备，不能作为纯推理耗时。音频通过波形、音高、响度等信号检查及浏览器加载验证，不声称进行了人工听音。部分完整回归与真实case同期运行，包含共享机器资源竞争，不作为生产性能基准。',
        '素材包含真实照片和为可重复校验合成的音视频；浏览器操作、部署服务、模型请求和交付文件均实际执行。视频内容抽帧使用从头解码后定位，避免跳转解码掩盖拼接参数错误；不将完整解码无报错等同于内容正确。',
        '外部服务的调用状态、任务ID和轮询记录见JSON报告；服务未返回的费用不推算为零。未完成、未知或质量未通过的调用不计通过。',
        '浏览器会话凭证不包含在报告中。每次失败及修复见 [问题记录](bugs.json)；可观测性实现与自测见 [实现报告](../observability-implementation-report.md)。','']
    (DOC/(suffix+'.md')).write_text('\n'.join(lines))
    print(json.dumps({'complete':complete,'report':str(DOC/(suffix+'.md'))}))
if __name__=='__main__':main()
