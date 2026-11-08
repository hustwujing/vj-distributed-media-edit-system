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

export function confirmationDetails(decision,assets=[]){
 if(decision.kind!=='plan_confirmation')return '';
 const request=decision.request;
 const inputs=step=>Object.entries(step.inputs||{}).map(([role,ref])=>`${role}：${ref.asset?(assets.find(asset=>asset.id===ref.asset)?.name||ref.asset):ref.step?`${ref.step} / ${ref.role}`:ref.repair_candidate?'当前修复候选':'未指定'}`).join('；');
 return `<section class="plan-confirmation-details"><p><strong>交付内容：</strong>${esc((request.delivery_targets||[]).join('、'))}</p><ol>${(request.steps||[]).map(step=>`<li><strong>${esc(step.name)}</strong><p>${esc(step.objective)}</p>${inputs(step)?`<p>使用素材：${esc(inputs(step))}</p>`:''}${step.parameters&&Object.keys(step.parameters).length?`<details><summary>关键参数</summary><pre>${esc(JSON.stringify(step.parameters,null,2))}</pre></details>`:''}</li>`).join('')}</ol></section>`;
}

export function candidateItems(run){
 return run.tasks.filter(task=>task.selected!==false).flatMap(task=>{
  const roles=task.quality?.output_roles||{},accepted=new Set(task.accepted_result||[]);
  return [...new Set(task.candidate_result||[])].filter(file=>!accepted.has(file)).map(file=>({
   name:file.split('/').at(-1),file,task_id:task.id,task_name:task.spec.name,revision:task.revision,
   reason:task.error_detail||task.error_code||'尚未完成验收',role:Object.keys(roles).find(role=>roles[role]===file)||'',
   url:`api/projects/${run.project_id}/runs/${run.id}/tasks/${task.id}/file?file=${encodeURIComponent(file)}`,
   subtitle:'候选文件 · 未正式交付',quality_state:'pending',workflow_delivery:false,
  }));
 }).sort((a,b)=>Number(['foreground','primary_image','video','audio','transcript'].includes(b.role))-Number(['foreground','primary_image','video','audio','transcript'].includes(a.role)));
}

export function renderCandidates(root,run,showPreview){
 const items=deliveryItems(candidateItems(run)).map(item=>({...item,subtitle:'候选文件 · 未正式交付'}));
 const key=JSON.stringify([run.id,items]);if(root.dataset.signature===key)return;
 root.dataset.signature=key;root.hidden=!items.length;
 root.innerHTML=items.length?`<h2>候选文件（未正式交付）</h2><p class="muted">可预览、下载或说明修改要求，尚未通过验收。</p>${items.map((item,index)=>`<article class="dynamic-deliverable"><button type="button" data-candidate-preview="${index}" aria-label="预览候选 ${esc(item.name)}">${icon('eye',16)}</button><span><strong>${esc(item.name)}</strong><small>${esc(item.task_name)} · ${esc(item.reason)}</small></span><a href="${esc(href(item.url))}" download aria-label="下载候选 ${esc(item.name)}">${icon('download',15)}</a></article>`).join('')}`:'';
 root.querySelectorAll('[data-candidate-preview]').forEach(button=>button.onclick=()=>showPreview({...items[Number(button.dataset.candidatePreview)],workflowDelivery:false}));
}

export function renderRunResult(root,run,showPreview){
 const items=deliveryItems(run.delivery),previous=deliveryItems(run.parent_result?.delivery);
 const pending=['WAITING_USER','FAILED','CANCELLED'].includes(run.state)?deliveryItems(candidateItems(run)).slice(0,4).map(item=>({...item,subtitle:'候选文件 · 未正式交付'})):[];
 const terminal=['SUCCEEDED','FAILED','CANCELLED'].includes(run.state);
 const key=JSON.stringify([run.id,run.state,run.terminal_notice,run.conversation_answer,run.terminal_reason,items,pending,run.parent_result]);
 if(root.dataset.signature===key)return false;
 root.dataset.signature=key;root.hidden=!terminal&&!previous.length&&!pending.length;
 const files=(values,prefix)=>values.map((item,index)=>`<div class="conversation-result-file"><button type="button" class="button secondary" data-result-preview="${prefix}${index}">${icon('play',14)}预览 ${esc(item.name)}</button><a class="button text" href="${esc(href(item.url))}" download>下载 ${esc(item.name)}</a></div>`).join('');
 const answered=run.terminal_reason==='conversation_answer';
 const title=answered?'答复':run.state==='SUCCEEDED'?'任务已完成':run.state==='FAILED'?'任务未完成':'任务已取消';
 const message=answered?run.conversation_answer:run.state==='SUCCEEDED'?(items.length?`已发布 ${items.length} 个正式成果，可直接预览或下载。`:'本轮已结束，未发布可下载成果。'):(run.terminal_notice||'本轮已停止，已有成果会保留。');
 root.innerHTML=(terminal?`<section class="conversation-result" role="status"><h3>${title}</h3><p>${esc(message)}</p>${files(items,'current-')}</section>`:'')+
  (previous.length?`<section class="conversation-result previous"><h3>上一轮成果仍可使用</h3><p>${esc(run.parent_result.goal)}</p>${files(previous,'parent-')}</section>`:'');
 if(pending.length)root.innerHTML+=`<section class="conversation-result previous"><h3>已有候选，尚未交付</h3><p>这些文件尚未通过验收，可先预览，再说明需要怎样修改。</p>${files(pending,'candidate-')}</section>`;
 root.querySelectorAll('[data-result-preview]').forEach(button=>button.onclick=()=>{const [kind,index]=button.dataset.resultPreview.split('-');showPreview({...((kind==='parent'?previous:kind==='candidate'?pending:items)[Number(index)]),workflowDelivery:kind!=='candidate'});});
 return true;
}
