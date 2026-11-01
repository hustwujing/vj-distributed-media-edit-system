export const stepLabels={pending:'待执行',submitting:'正在提交',waiting:'等待外部结果',running:'处理中',collecting:'接收结果',verifying:'程序验收中',agent_review:'Agent 验收中',diagnosing:'Agent 诊断中',awaiting_review:'等待质量确认',succeeded:'验收通过',paused:'已暂停',cancelled:'已停止'};
export const cleanupLabels={not_allocated:'尚未分配执行资源',allocated:'执行资源使用中',pending:'准备回收资源',cleaning:'资源回收中',quarantined:'资源回收异常，保持隔离',complete:'本次执行资源已回收'};
export function qualityActions(step,execution){
 if(step.status!=='awaiting_review'||execution?.termination_reason)return [];
 const result=['reject'];
 if(step.quality?.technical_valid&&step.quality?.passed)result.unshift('approve');
 return result;
}
export function lifecycleText(execution){
 if(!execution)return '';
 const wait={dependency:'等待前置步骤',resource:'等待计算资源',provider:'等待外部结果',retry:'等待重试',user:'等待你的决定',model_unconfigured:'模型未配置',node_limit:'等待节点执行槽',user_limit:'等待用户执行槽',project_busy:'项目正在处理其他任务',cleanup:'等待资源清理',paused:'已暂停'}[execution.scheduling?.wait_reason]||'';
 const since=execution.scheduling?.wait_recorded_at;
 const waiting=wait&&since?`（本次等待已记录 ${Math.max(0,Math.floor(Date.now()/1000-since))} 秒）`:'';
 const cleanup=cleanupLabels[execution.cleanup?.state]||'';
 const reasons={deadline:'任务已超过总时限',budget:'任务运行预算已用完',security:'任务已因安全策略停止',cancelled:'任务已停止'};
 const external=execution.external_cleanup||[];
 const suffix=external.some(x=>x.state==='unsupported')?' · 上游不支持取消，任务可能继续运行':external.some(x=>['unknown','unconfirmed'].includes(x.state))?' · 上游是否停止尚未确认':external.some(x=>['pending','running'].includes(x.state))?' · 正在确认上游停止':'';
 if(execution.termination_reason)return `${reasons[execution.termination_reason]||'任务已停止'} · ${cleanup}${suffix}`;
 const minutes=Math.max(0,Math.ceil((execution.remaining_seconds||0)/60));
 return `剩余运行预算 ${minutes} 分钟 · ${cleanup}${wait?' · '+wait+waiting:''}${suffix}`;
}

export function jobPhaseLabel(job){return ({queued:'排队中',pending:'准备处理',submitting:'正在提交',waiting:'等待外部结果',collecting:'接收结果',running:'处理中',succeeded:'已完成',failed:'未完成',cancelled:'已停止'})[job.phase||job.status]||'处理中';}

export function pauseReason(workflow){
 const block=workflow?.resume_block;
 if(!block)return '';
 const scope={task:'本任务',daily:'今日',turn:'本轮'}[block.scope]||'当前';
 const category=block.category==='model'?'模型':'工具';
 const counts=Number.isFinite(block.used)&&Number.isFinite(block.limit)?`（${block.used}/${block.limit}）`:'';
 const advice=block.scope==='daily'?'请等待每日额度恢复或联系管理员。':'请停止当前任务后确认新的收尾方案。';
 return `${scope}${category}调用额度已用完${counts}。已完成成果已保留，${advice}`;
}
