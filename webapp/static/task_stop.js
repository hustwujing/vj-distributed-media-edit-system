// Cancellation acknowledgement and resource cleanup are separate states.
const active=new Set(['queued','running','waiting','paused','cancelling']);
export function stopState(turn,phase=''){
 const state=(label,description,busy=true)=>({visible:true,disabled:true,busy,label,description});
 if(!turn)return {visible:false,disabled:true,busy:false,label:'停止',description:''};
 if(phase==='confirming')return state('等待确认','请确认是否停止这个任务。',false);
 if(phase==='submitting')return state('停止中…','正在提交停止请求。');
 if(turn.status==='cancelling'||phase==='awaiting'&&active.has(turn.status))return state('停止中…','停止请求已提交，正在等待服务端确认。');
 if(active.has(turn.status))return {visible:true,disabled:false,busy:false,label:'停止',description:'停止当前任务，保留已经保存的成果。'};
 if(turn.status==='cancelled'){
  const cleanup=turn.execution?.cleanup?.state,external=turn.execution?.external_cleanup||[];
  if(cleanup==='quarantined')return state('回收待处理','资源回收遇到问题，已隔离并等待处理。',false);
  if(['allocated','pending','cleaning'].includes(cleanup))return state('收尾中…','已停止后续执行，正在回收本机资源。');
  if(!['complete','not_allocated'].includes(cleanup))return state('停止待确认','尚未确认本机资源已释放，请查看任务进度。',false);
  if(external.some(x=>['pending','running'].includes(x.state)))return state('确认停止中…','本机任务已停止，正在确认外部服务是否结束。');
  if(external.some(x=>x.state!=='complete'))return state('停止待确认','本机任务已停止，上游是否结束尚未确认。',false);
  return {visible:false,disabled:true,busy:false,label:'已停止',description:'任务已停止；已经保存的成果会保留。'};
 }
 return {visible:false,disabled:true,busy:false,label:'停止',description:'该任务已结束，无需停止。'};
}
