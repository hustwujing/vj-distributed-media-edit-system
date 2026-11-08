import {esc,href,icon} from './common.js';

export function deliveryItems(delivery=[]){
 return delivery.map(item=>({...item,type:item.type||item.mime||({mp4:'video/mp4',mov:'video/quicktime',png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',webp:'image/webp',wav:'audio/wav',mp3:'audio/mpeg',json:'application/json'}[item.name?.split('.').pop()?.toLowerCase()]||'application/octet-stream'),subtitle:`正式交付${item.role?' · '+item.role:''}`}));
}

export function renderPlanSummary(root,run){
 const tasks=run.tasks.filter(item=>item.selected!==false),key=JSON.stringify([run.id,run.graph_version,run.authorization?.delivery_targets,tasks.map(item=>[item.id,item.spec.name,item.spec.objective,item.status])]);
 if(root.dataset.signature===key)return false;
 const sameRun=root.dataset.run===run.id,open=root.querySelector('details')?.open;
 root.dataset.signature=key;root.dataset.run=run.id;root.hidden=!tasks.length;
 root.innerHTML=tasks.length?`<details class="conversation-plan" ${sameRun?open?'open':'':'open'}><summary>执行方案 · ${tasks.length} 步</summary><p>交付内容：${esc((run.authorization?.delivery_targets||[]).join('、')||'未记录')}</p><ol>${tasks.map(item=>`<li><strong>${esc(item.spec.name)}</strong>${item.spec.objective?`<p>${esc(item.spec.objective)}</p>`:''}</li>`).join('')}</ol></details>`:'';
 return true;
}

export function confirmationDetails(decision){
 if(decision.kind!=='plan_confirmation')return '';
 const request=decision.request;
 return `<section class="plan-confirmation-details"><p><strong>交付内容：</strong>${esc((request.delivery_targets||[]).join('、'))}</p><ol>${(request.steps||[]).map(step=>`<li><strong>${esc(step.name)}</strong><p>${esc(step.objective)}</p>${step.parameters&&Object.keys(step.parameters).length?`<details><summary>关键参数</summary><pre>${esc(JSON.stringify(step.parameters,null,2))}</pre></details>`:''}</li>`).join('')}</ol></section>`;
}

export function renderRunResult(root,run,showPreview){
 const items=deliveryItems(run.delivery),previous=deliveryItems(run.parent_result?.delivery);
 const terminal=['SUCCEEDED','FAILED','CANCELLED'].includes(run.state);
 const key=JSON.stringify([run.id,run.state,run.terminal_notice,run.conversation_answer,run.terminal_reason,items,run.parent_result]);
 if(root.dataset.signature===key)return false;
 root.dataset.signature=key;root.hidden=!terminal&&!previous.length;
 const files=(values,prefix)=>values.map((item,index)=>`<div class="conversation-result-file"><button type="button" class="button secondary" data-result-preview="${prefix}${index}">${icon('play',14)}预览 ${esc(item.name)}</button><a class="button text" href="${esc(href(item.url))}" download>下载 ${esc(item.name)}</a></div>`).join('');
 const answered=run.terminal_reason==='conversation_answer';
 const title=answered?'答复':run.state==='SUCCEEDED'?'任务已完成':run.state==='FAILED'?'任务未完成':'任务已取消';
 const message=answered?run.conversation_answer:run.state==='SUCCEEDED'?(items.length?`已发布 ${items.length} 个正式成果，可直接预览或下载。`:'本轮已结束，未发布可下载成果。'):(run.terminal_notice||'本轮已停止，已有成果会保留。');
 root.innerHTML=(terminal?`<section class="conversation-result" role="status"><h3>${title}</h3><p>${esc(message)}</p>${files(items,'current-')}</section>`:'')+
  (previous.length?`<section class="conversation-result previous"><h3>上一轮成果仍可使用</h3><p>${esc(run.parent_result.goal)}</p>${files(previous,'parent-')}</section>`:'');
 root.querySelectorAll('[data-result-preview]').forEach(button=>button.onclick=()=>{const [kind,index]=button.dataset.resultPreview.split('-');showPreview({...((kind==='parent'?previous:items)[Number(index)]),workflowDelivery:true});});
 return true;
}
