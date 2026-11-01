export const actorLabels={light_agent:'轻量 Agent',sandbox_agent:'沙盒 Agent',workflow_sandbox:'工作流沙盒',host:'宿主程序',media_executor:'媒体执行器',external_service:'外部服务'};
const active=s=>['queued','running','waiting','cancelling'].includes(s);
const priority={executing:5,writing:5,inspecting:5,reading:5,operation:5,local:5,checkpoint:4,prepare_tool:3,responding:3,analysis:2,model_wait:1};
export function runningWork(turn,items){
 if(!active(turn.status))return [];
 const groups=new Map();
 for(const item of items){if(item.status!=='running')continue;const attempt=item.attempt||item.execution?.attempt_id;const known=(turn.activity_attempts||[]).find(x=>x.id===attempt);if(known&&known.status!=='running')continue;groups.set(item.id,item);}

 for(const step of turn.workflow?.steps||[]){if(step.status==='waiting'&&step.wait_reason==='provider')groups.set('external:'+step.id,{id:'external:'+step.id,actor:'external_service',phase:'waiting',label:'等待上游任务完成',operation:step.operation,step:step.id,status:'running',updated:step.updated||0});}
 return [...groups.values()].sort((a,b)=>(b.updated||0)-(a.updated||0));
}
export function workLabel(item){return (actorLabels[item.actor]||'执行器')+' · '+item.label+(item.operation&&!item.label.includes(item.operation)?' · '+item.operation:'');}

export function mergeWork(old,next){
 if(!old)return next;
 // Lifecycle and logical sequence are authoritative. SDK execution timestamps
 // and transport receipt timestamps may differ even for zero-duration tools.
 if(old.status!=='running'&&next.status==='running')return old;
 const prior=old.sequence||0,following=next.sequence||0;
 if(following!==prior)return following>prior?next:old;
 if(old.status==='running'&&next.status!=='running')return next;
 if(next.updated<old.updated)return old;
 return next;
}

export function settleTool(turn,item){
 if(item.status!=='running')return item;
 const attempt=item.execution?.attempt_id||item.attempt;
 const known=(turn.activity_attempts||[]).find(x=>x.id===attempt);
 if(['succeeded','failed','cancelled'].includes(turn.status)||(known&&known.status!=='running'))return {...item,status:'interrupted'};
 return item;
}
