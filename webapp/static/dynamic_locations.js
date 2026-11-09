const locations={
 control_host:['控制面宿主机','控制面','host'],
 agent_worker:['Agent Worker 宿主机','宿主','host'],
 executor_host:['Executor 宿主机','宿主','host'],
 bwrap_sandbox:['bwrap 沙箱','bwrap','local'],
 opensandbox:['OpenSandbox 沙箱','OpenSandbox','remote'],
 unallocated:['待分配','待分配','pending'],
 unrecorded:['执行位置未记录','未记录','unknown'],
 opensandbox_unconfirmed:['OpenSandbox（实例未确认）','实例待确认','pending'],
};
export function executionLocation(value){
 const [label,short,kind]=locations[value]||locations.unrecorded;
 return {label,short,kind};
}
const providerStates={intent:'提交意图已记录',unknown:'提交结果未确认',waiting:'已受理，等待处理',running:'处理中',succeeded:'已完成',failed:'失败',cancelled:'已取消',rejected:'被拒绝'};
export function providerExecution(value){return providerStates[value]||'状态未确认';}
export function executionPlace(item,aggregate=false){
 if(aggregate)return {label:'控制面',kind:'host',detail:'本轮汇总由宿主控制面调度；各任务实际执行位置见对应节点。'};
 const location=executionLocation(item.execution_location),tool=item.tool_location?executionLocation(item.tool_location):null;
 let label=location.short,kind=location.kind;
 const parts=[`程序执行：${location.label}`];
 if(tool){parts.push(`工具执行：${tool.label}`);if(tool.kind==='local'||tool.kind==='remote'){
  if(item.tool_location!==item.execution_location)label+=` · ${tool.short}`;
 }}
 const provider=item.provider_execution_state;
 if(provider){
  parts.push(`外部服务：${providerExecution(provider)}`);
  if(item.status==='waiting'&&['waiting','running'].includes(provider)){label='外部服务';kind='remote';}
 }
 return {label,kind,detail:`最近尝试；${parts.join('；')}`};
}
