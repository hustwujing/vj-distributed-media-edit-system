const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const terminal=state=>['SUCCEEDED','FAILED','CANCELLED'].includes(state);
export function duration(seconds){
 const value=Math.max(0,Math.floor(seconds));
 if(value<60)return `${value}秒`;
 if(value<3600)return `${Math.floor(value/60)}分${value%60}秒`;
 return `${Math.floor(value/3600)}小时${Math.floor(value/60)%60}分${value%60}秒`;
}
export function timingLabel(record,now=Date.now()/1000){
 if(!Number.isFinite(record?.started))return '暂无执行记录';
 const end=record.finished;
 if(Number.isFinite(end)){
  const outcome=['expired','interrupted'].includes(record.status)?'中断':'结束';
  return `${duration(end-record.started)} · ${outcome} ${new Date(end*1000).toLocaleTimeString('zh-CN',{hour12:false})}`;
 }
 return record.status==='running'?`已执行 ${duration(now-record.started)}`:'结束回执未记录';
}
export function timingMarkup(record,prefix=''){
 if(!record)return '<time class="execution-time">暂无执行记录</time>';
 const data=esc(JSON.stringify(record)),text=prefix+timingLabel(record);
 return `<time class="execution-time" data-execution-clock="${data}" data-clock-prefix="${esc(prefix)}" title="${esc(text)}">${esc(text)}</time>`;
}
export function executionHistory(item,run){
 return (run.activity_attempts||[]).filter(a=>a.step_id===item.id&&a.task_revision===(item.revision??0))
  .sort((a,b)=>a.generation-b.generation||a.started-b.started||a.id.localeCompare(b.id))
  .map(a=>({...a,status:terminal(run.state)&&item.control_kind!=='MESSAGE'&&a.finished==null?'unconfirmed':a.status}));
}
export function executionTimes(item,run){
 const history=executionHistory(item,run);
 return '<span class="execution-times">'+(history.length?history.slice(-3).map((a,i)=>
  timingMarkup(a,`第${Math.max(0,history.length-3)+i+1}次 · `)).join(''):timingMarkup(null))+'</span>';
}
export function runTiming(run){
 return {started:run.created_at,finished:terminal(run.state)?run.delivered_at||run.updated_at:null,status:'running'};
}
export function updateExecutionClocks(root=document,now=Date.now()/1000){
 for(const node of root.querySelectorAll('[data-execution-clock]')){
  const text=(node.dataset.clockPrefix||'')+timingLabel(JSON.parse(node.dataset.executionClock),now);
  if(node.textContent!==text){node.textContent=text;node.title=text;}
 }
}
