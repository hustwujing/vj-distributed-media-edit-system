import {$,api,header,esc,href,toast} from './common.js';
import {terminalRun,canApproveDecision,acceptsFeedback,inputStatus} from './dynamic_state.js';
const projectId=document.body.dataset.project;
const path=suffix=>`api/projects/${projectId}${suffix}`;
const names={ACTIVE:'进行中',WAITING_USER:'等待处理',CANCELLING:'正在取消',CANCELLED:'已取消',SUCCEEDED:'已完成',FAILED:'未完成',PLANNING:'正在规划',EXECUTING:'正在执行',REVIEWING:'正在审查',IDLE:'暂无执行活动',planning:'正在规划',running:'执行中',ready:'等待执行',blocked:'等待前置结果',leased:'已分配执行器',waiting:'等待外部结果',retry_wait:'等待安全重试',reviewing:'验收中',waiting_user:'等待你的确认',paused:'已暂停',cancelling:'正在取消',cancelled:'已取消',succeeded:'已完成',failed:'未完成'};
const labels={input_accepted:'补充要求已接收',input_safe_point:'当前任务已排空，开始重新规划',input_batch_applied:'补充要求已处理',manager_result_applied:'规划结果已应用',review_reused_or_blocked:'候选与证据未变，保留已有审查结论',successor_created:'已创建下一轮执行',proposal_applied:'新一轮计划已确定',planning_requested:'开始下一轮规划',proposal_rejected:'提案未通过校验，正在处理',review_requested:'开始验收',task_result_committed:'步骤结果已记录',state_changed:'工作流状态更新',command_processed:'控制请求已处理',delivered:'正式成果已交付',created:'工作流已创建'};
let project,capabilities,currentId=null,current=null,timer=null,busy=false,lastRender='',lastDecisions='',stopped=false,pendingCommand=null,uploading=0,projectLoad=0,capabilityLoad=0,refreshEpoch=0,lastSnapshot=null;
// getRandomValues also works on the HTTP intranet entry, unlike randomUUID.
const uuid=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
const draftKey=(run,kind)=>`dynamic-draft:${projectId}:${run}:${kind}`;
function readDraft(run,kind){try{return JSON.parse(sessionStorage.getItem(draftKey(run,kind))||'null');}catch{return null;}}
function writeDraft(run,kind,value){try{if(value)sessionStorage.setItem(draftKey(run,kind),JSON.stringify(value));else sessionStorage.removeItem(draftKey(run,kind));}catch{/* Storage may be disabled; editing remains available. */}}
function saveInputDraft(){const form=$('dynamic-input-form');if(!form.dataset.run)return;
 writeDraft(form.dataset.run,'input',{text:$('dynamic-input-content').value,assets:[...$('dynamic-input-assets').selectedOptions].map(x=>x.value),targets:[...$('dynamic-input-targets').selectedOptions].map(x=>x.value),requestId:form.dataset.requestId});}
function error(message){$('dynamic-error').textContent=message;$('dynamic-error').hidden=!message;}
function safeName(task){return task.spec.name;}
async function loadProject(){const epoch=++projectLoad;const value=await api(path(''));if(epoch!==projectLoad)return;
 const previous=[...$('dynamic-assets').querySelectorAll('input')],known=new Set(previous.map(x=>x.value)),selected=new Set(previous.filter(x=>x.checked).map(x=>x.value));
 project=value;header('chat',project);$('dynamic-assets').innerHTML=project.assets.map(a=>`<label><input type="checkbox" value="${esc(a.id)}" ${!known.has(a.id)||selected.has(a.id)?'checked':''}>${esc(a.name)}</label>`).join('')||'<p>可上传素材，也可直接创建生成任务。</p>';if(!$('dynamic-goal').value)$('dynamic-goal').value=project.draft?.prompt||'';}
async function loadCapabilities(){const epoch=++capabilityLoad;const value=await api(path('/runs/capabilities'));if(epoch!==capabilityLoad)return;
 const limits=Object.fromEntries([...document.querySelectorAll('[data-operation]')].map(input=>[input.dataset.operation,input.value]));capabilities=value;
 $('dynamic-provider-limits').innerHTML=capabilities.operations.filter(op=>op.external_side_effects).map(op=>`<label class="dynamic-scope-item"><span>${esc(op.id)}${op.available?'':'（未配置）'}</span><input type="number" min="0" max="10" value="${esc(limits[op.id]??'0')}" data-operation="${esc(op.id)}" data-budget-group="${esc(op.budget_group)}" ${op.available?'':'disabled'} aria-label="${esc(op.id)} 调用上限"></label>`).join('');}
function authorization(){const budgets={};document.querySelectorAll('[data-operation]').forEach(input=>{const n=Number(input.value);if(n>0)budgets[input.dataset.budgetGroup]=(budgets[input.dataset.budgetGroup]||0)+n;});return {goal:$('dynamic-goal').value.trim(),allowed_assets:[...$('dynamic-assets').querySelectorAll('input:checked')].map(x=>x.value),allowed_operations:['script',...capabilities.operations.filter(op=>!op.external_side_effects&&op.available).map(op=>op.id),...[...document.querySelectorAll('[data-operation]')].filter(x=>Number(x.value)>0).map(x=>x.dataset.operation)],allowed_agent_profiles:['editing_implementer','media_failure_diagnostician','visual_quality_reviewer'],provider_call_budget:budgets,delivery_targets:$('dynamic-targets').value.split(/[\n,，]/).map(x=>x.trim()).filter(Boolean),wall_time_budget:Number($('dynamic-time').value)*60,model_token_budget:Number($('dynamic-token-budget').value),content_fingerprint:capabilities.content_fingerprint};}
$('dynamic-start').onsubmit=async event=>{event.preventDefault();if(busy||uploading)return;busy=true;$('dynamic-submit').disabled=true;error('');try{const fresh=await api(path('/runs/capabilities'));if(fresh.content_fingerprint!==capabilities.content_fingerprint){capabilities.content_fingerprint=fresh.content_fingerprint;throw new Error('素材或工作区内容已变化，请检查当前执行范围后再次确认。');}const result=await api(path('/runs'),{method:'POST',body:JSON.stringify({request_id:uuid(),confirmed:true,authorization:authorization(),parent_run_id:current?.id||null})});currentId=result.id;lastRender='';await refresh();}catch(e){error(e.message);}finally{busy=false;$('dynamic-submit').disabled=uploading>0;}};
$('dynamic-upload').onchange=async event=>{const input=event.target,files=[...input.files];uploading++;document.body.dataset.uploading='true';input.disabled=true;$('dynamic-submit').disabled=true;input.parentElement.firstChild.nodeValue='正在上传素材…';
 try{for(const file of files){const form=new FormData();form.append('file',file);const response=await fetch(href(path('/assets')),{method:'POST',body:form,credentials:'same-origin'});if(!response.ok)throw new Error('素材上传失败');}
  await loadProject();await loadCapabilities();
 }catch(e){toast(e.message,true);}finally{input.value='';uploading--;document.body.dataset.uploading=String(uploading>0);input.disabled=uploading>0;input.parentElement.firstChild.nodeValue='上传素材';$('dynamic-submit').disabled=busy||uploading>0;}};
async function command(action,payload={},requestId=uuid()){if(!current)return;const submitted=await api(path(`/runs/${current.id}/commands`),{method:'POST',body:JSON.stringify({request_id:requestId,action,payload})});pendingCommand=submitted.id;toast(action==='cancel'?'停止请求已提交，正在回收本地资源':'请求已提交，正在处理');await refresh();}
document.querySelectorAll('[data-control]').forEach(button=>button.onclick=async()=>{const action=button.dataset.control;if(action==='cancel'&&!window.confirm('确定停止这个工作流？已完成成果会保留，本地任务将回收；外部请求若已提交，会单独核实其状态。'))return;button.disabled=true;try{await command(action);}catch(e){toast(e.message,true);}finally{button.disabled=false;}});
function syncAssetOptions(select){
 const known=new Set([...select.options].map(option=>option.value));
 for(const asset of project.assets)if(!known.has(asset.id)){const option=document.createElement('option');option.value=asset.id;option.textContent=asset.name;select.append(option);}
}
function renderDecisions(run){
 const identity=JSON.stringify(run.decisions);
 const root=$('dynamic-decisions');
 if(identity===lastDecisions){root.querySelectorAll('[data-feedback-assets]').forEach(syncAssetOptions);return;}
 const drafts=new Map([...root.children].map(element=>[element.dataset.decision,{text:element.querySelector('textarea')?.value||'',assets:[...(element.querySelector('[data-feedback-assets]')?.selectedOptions||[])].map(option=>option.value),upstreams:[...(element.querySelector('[data-upstreams]')?.selectedOptions||[])].map(option=>option.value),requestId:element.dataset.requestId}]));
 lastDecisions=identity;
 root.innerHTML=run.decisions.map(d=>`<article class="dynamic-decision" data-decision="${d.id}"><h3>${['plan_change','successor_authorization','new_run'].includes(d.kind)?'请确认执行范围':d.kind==='input_question'?'需要补充信息':'请检查当前候选'}</h3><p>${esc(d.request.reason||'请确认候选是否满足你的要求。')}</p>${d.request.question?`<p>${esc(d.request.question)}</p>`:''}${d.request.options?.length?`<p>可选：${d.request.options.map(esc).join(' / ')}</p>`:''}<div class="scope-diff"></div>${acceptsFeedback(d)?'<label class="field">修改意见<textarea data-feedback rows="3" maxlength="4000"></textarea></label><label class="field">关联素材<select data-feedback-assets multiple></select></label><label class="field">明确需要一并修改的上游步骤（可不选）<select data-upstreams multiple></select></label><button class="button primary" data-answer="revise">按意见修改并继续</button> ':''}${canApproveDecision(d,run)?'<button class="button primary" data-answer="approve">批准并继续</button> ':''}<button class="button secondary" data-answer="reject">停止当前方向</button></article>`).join('');
 for(const element of root.children){
  const d=run.decisions.find(item=>item.id===element.dataset.decision);
  let proposed=structuredClone(d.request.authorization||run.authorization);
  if(d.kind==='new_run')proposed.goal=d.request.new_goal;
  if(d.request.proposal){
   for(const task of d.request.proposal.add_tasks||[]){
    if(task.on_failure==='diagnose'&&!proposed.allowed_agent_profiles.includes('media_failure_diagnostician'))proposed.allowed_agent_profiles.push('media_failure_diagnostician');
    if(task.type==='agent'){if(!proposed.allowed_agent_profiles.includes(task.profile))proposed.allowed_agent_profiles.push(task.profile);}
    else{const name=task.type==='script'?'script':task.operation;if(!proposed.allowed_operations.includes(name))proposed.allowed_operations.push(name);const operation=capabilities.operations.find(item=>item.id===name);if(operation?.external_side_effects)proposed.provider_call_budget[operation.budget_group]=(proposed.provider_call_budget[operation.budget_group]||0)+1;}
    for(const ref of Object.values(task.inputs||{}))if(ref.asset&&!proposed.allowed_assets.includes(ref.asset))proposed.allowed_assets.push(ref.asset);
   }
  }
  const scope=element.querySelector('.scope-diff');
  if(['plan_change','successor_authorization','new_run'].includes(d.kind))scope.textContent=`目标：${proposed.goal}；交付目标：${proposed.delivery_targets.join("、")}；模型 Token 上限：${proposed.model_token_budget}；时限：${proposed.wall_time_budget/60} 分钟；任务/规划上限：${proposed.max_tasks}/${proposed.max_planning_rounds}；素材：${proposed.allowed_assets.map(id=>project.assets.find(asset=>asset.id===id)?.name||id).join('、')||'无'}；允许能力：${proposed.allowed_operations.join('、')}；专业 Agent：${proposed.allowed_agent_profiles.join('、')}；外部调用总上限：${JSON.stringify(proposed.provider_call_budget)}；素材版本：${proposed.content_fingerprint.slice(0,12)}`;
  if(acceptsFeedback(d)){
   const draft=drafts.get(d.id)||readDraft(run.id,d.id);const select=element.querySelector('[data-feedback-assets]');syncAssetOptions(select);
   if(draft){element.querySelector('textarea').value=draft.text;for(const option of select.options)option.selected=draft.assets.includes(option.value);}
   const ancestors=new Set(),pending=[d.request.task_id];while(pending.length){const child=pending.pop();for(const edge of run.dependencies)if(edge.downstream_task_id===child&&!ancestors.has(edge.upstream_task_id)){ancestors.add(edge.upstream_task_id);pending.push(edge.upstream_task_id);}}
   const upstreams=element.querySelector('[data-upstreams]');for(const id of ancestors){const option=document.createElement('option');option.value=id;option.textContent=run.tasks.find(task=>task.id===id)?.spec.name||id;upstreams.append(option);}
   if(draft){for(const option of upstreams.options)option.selected=(draft.upstreams||[]).includes(option.value);if(draft.requestId)element.dataset.requestId=draft.requestId;}
   upstreams.parentElement.hidden=!ancestors.size;
  }
  const saveFeedback=()=>writeDraft(run.id,d.id,{text:element.querySelector('textarea')?.value||'',assets:[...(element.querySelector('[data-feedback-assets]')?.selectedOptions||[])].map(x=>x.value),upstreams:[...(element.querySelector('[data-upstreams]')?.selectedOptions||[])].map(x=>x.value),requestId:element.dataset.requestId});
  element.querySelectorAll('textarea,select').forEach(input=>input.addEventListener('input',()=>{delete element.dataset.requestId;saveFeedback();}));
  element.querySelectorAll('[data-answer]').forEach(button=>button.onclick=async()=>{
   button.disabled=true;
   try{
    const payload={decision_id:d.id,snapshot_digest:d.snapshot_digest,action:button.dataset.answer};
    if(payload.action==='revise'){
     payload.feedback=element.querySelector('textarea').value.trim();if(!payload.feedback)throw new Error('请填写具体修改意见。');
     payload.attachments=[...element.querySelector('[data-feedback-assets]').selectedOptions].map(option=>option.value);
     payload.upstream_tasks=[...element.querySelector('[data-upstreams]').selectedOptions].map(option=>option.value);
    }
    if(['plan_change','successor_authorization','new_run'].includes(d.kind)&&payload.action==='approve'){
     const fresh=await api(path('/runs/capabilities'));
     if(fresh.content_fingerprint!==proposed.content_fingerprint){proposed.content_fingerprint=fresh.content_fingerprint;scope.textContent+='；素材版本已更新为 '+fresh.content_fingerprint.slice(0,12)+'，请核对后再次确认。';throw new Error('素材已变化，请核对当前范围后再次确认。');}
     payload.authorization={...proposed,allowed_asset_digests:{}};
    }
    element.dataset.requestId ||= uuid();saveFeedback();await command('answer',payload,element.dataset.requestId);
   }catch(e){toast(e.message,true);}finally{button.disabled=false;}
  });
 }
}
$('dynamic-input-form').onsubmit=async event=>{
 event.preventDefault();if(!current)return;const content=$('dynamic-input-content').value.trim();if(!content)return;
 const origin=current.id,attachments=[...$('dynamic-input-assets').selectedOptions].map(option=>option.value),targets=[...$('dynamic-input-targets').selectedOptions].map(option=>option.value);
 const input=$('dynamic-input-content');const key=$('dynamic-input-form').dataset.requestId||uuid();$('dynamic-input-form').dataset.requestId=key;saveInputDraft();$('dynamic-input-submit').disabled=true;
 try{const result=await api(path(`/runs/${origin}/inputs`),{method:'POST',body:JSON.stringify({request_id:key,content,attachments,targets})});
  if(input.value.trim()===content&&JSON.stringify([...$('dynamic-input-assets').selectedOptions].map(option=>option.value))===JSON.stringify(attachments)&&JSON.stringify([...$('dynamic-input-targets').selectedOptions].map(option=>option.value))===JSON.stringify(targets)){
   input.value='';delete $('dynamic-input-form').dataset.requestId;saveInputDraft();
  }
  if(currentId===origin)currentId=result.run_id;toast('补充要求已接收，正在等待安全的处理时机');await refresh();
 }catch(e){toast(e.message,true);}finally{$('dynamic-input-submit').disabled=false;}
};
for(const [id,event] of [['dynamic-input-content','input'],['dynamic-input-assets','change'],['dynamic-input-targets','change']])$(id).addEventListener(event,()=>{delete $('dynamic-input-form').dataset.requestId;saveInputDraft();});

function render(run){if(lastSnapshot?.id===run.id&&(run.updated_at<lastSnapshot.updated_at||run.graph_version<lastSnapshot.graph_version))return;lastSnapshot={id:run.id,updated_at:run.updated_at,graph_version:run.graph_version};current=run;const receipt=(run.commands||[]).find(c=>c.id===pendingCommand&&c.status!=='pending');if(receipt){pendingCommand=null;if(receipt.status==='applied'&&run.successor_run_id){currentId=run.successor_run_id;lastDecisions='';lastSnapshot=null;queueMicrotask(refresh);return;}if(receipt.status==='rejected')toast(({replan_required:'当前结果需要重新规划。',external_result_requires_resolution:'需要先核实外部请求及结果，不能重复调用。',invalid_or_stale_decision:'确认信息不完整或已过期，请按当前状态重新确认。',workflow_terminal:'本轮已经结束。',cancellation_in_progress:'取消正在处理中。'})[receipt.error_code]||'控制请求未通过，请核对当前状态。',true);else toast('控制请求已处理');}$('dynamic-current').dataset.runId=run.id;$('dynamic-current').dataset.status=run.state;$('dynamic-current').dataset.graphVersion=String(run.graph_version);$('dynamic-current').hidden=false;$('dynamic-title').textContent=run.goal;const cleanup=run.state==='CANCELLING'?` · 待回收 ${run.cleanup_pending} 项`:'';$('dynamic-status').textContent=`${names[run.state]||run.state} · ${names[run.activity]||run.activity}${cleanup} · 计划版本 ${run.graph_version} · ${run.sealed?'计划已封口':'根据结果继续规划'} · 模型预算剩余 ${run.token_usage.remaining}/${run.token_usage.limit} tokens${run.token_usage.reserved_or_unknown?`（${run.token_usage.reserved_or_unknown} 待核实）`:""}`;$('dynamic-updated').textContent=`最后更新 ${new Date(run.updated_at*1000).toLocaleString()}`;$('dynamic-diagnostics').href=href(path(`/runs/${run.id}/diagnostics?download=true`));
 if($('dynamic-input-form').dataset.run!==run.id)saveInputDraft();
 const active=!terminalRun(run.state);$('dynamic-start').hidden=active;
 document.querySelector('[data-control=pause]').hidden=!active||['WAITING_USER','CANCELLING'].includes(run.state);
 document.querySelector('[data-control=resume]').hidden=run.state!=='WAITING_USER';document.querySelector('[data-control=cancel]').hidden=!active;
 $('dynamic-input-form').hidden=['FAILED','CANCELLED','CANCELLING'].includes(run.state);syncAssetOptions($('dynamic-input-assets'));
 $('dynamic-input-status').textContent=inputStatus(run);
 const choices=$('dynamic-input-targets');choices.parentElement.hidden=terminalRun(run.state);
 if(terminalRun(run.state))for(const option of choices.options)option.selected=false;
 const known=new Map([...choices.options].map(option=>[option.value,option]));
 for(const task of run.tasks.filter(item=>item.selected)){if(!known.has(task.id)){const option=document.createElement('option');option.value=task.id;option.textContent=task.spec.name;choices.append(option);}known.delete(task.id);}
 for(const option of known.values())option.remove();
 const form=$('dynamic-input-form');if(form.dataset.run!==run.id){form.dataset.run=run.id;const draft=readDraft(run.id,'input')||{};
  $('dynamic-input-content').value=draft.text||'';delete form.dataset.requestId;if(draft.requestId)form.dataset.requestId=draft.requestId;
  for(const option of $('dynamic-input-assets').options)option.selected=(draft.assets||[]).includes(option.value);
  for(const option of choices.options)option.selected=!terminalRun(run.state)&&(draft.targets||[]).includes(option.value);
 }
 $('dynamic-input-history').innerHTML=(run.inputs||[]).map(item=>`<p>补充 ${item.sequence} · ${{ACCEPTED:'已接收',PLANNING:'正在规划',APPLIED:'已处理',SUPERSEDED:'已交给下一轮',FAILED:'处理受阻'}[item.status]||esc(item.status)} · ${esc(item.content)}</p>`).join('');
 $('dynamic-control-jobs').innerHTML=run.control_jobs.map(job=>`<p>${{PLAN:'规划',REVIEW:'审查',DIAGNOSE:'诊断'}[job.control_kind]} · ${names[job.status]||esc(job.status)}${job.error_code?' · '+esc(job.error_code):''}</p>`).join('');

 const signature=JSON.stringify(run.tasks);if(signature!==lastRender){lastRender=signature;$('dynamic-tasks').innerHTML=run.tasks.map(t=>{const semantics=t.spec.semantics||{};const semanticText=[semantics.subject?.mode,semantics.consistency_mode,semantics.mask_repair?`蒙版 ${semantics.mask_repair.level}`:'',...(semantics.temporal||[]).map(x=>`${x.action} · ${x.transition}`)].filter(Boolean).join(' · ');return `<article class="dynamic-task" data-status="${esc(t.status)}"><header><strong>${esc(safeName(t))}</strong><span>${t.selected?(names[t.status]||esc(t.status)):'已由新步骤替代（保留历史）'}</span></header><p>${esc(t.spec.objective||'')}</p><small>${esc(t.type)} · 修订 ${t.revision} · 调度 ${t.attempts}${t.wait_reason?' · '+esc(t.wait_reason):''}</small>${semanticText?`<p class="muted">${esc(semanticText)}</p>`:''}${t.error_code?`<p role="status">${esc(t.error_detail||t.error_code)}</p>`:''}${[...new Set([...t.candidate_result,...t.accepted_result])].map(file=>`<p><a target="_blank" rel="noopener" href="${esc(href(path(`/runs/${run.id}/tasks/${t.id}/file?file=${encodeURIComponent(file)}`)))}">${t.accepted_result.includes(file)?'已验收':'待验收'} · ${esc(file.split('/').at(-1))}</a>${t.accepted_result.includes(file)?` <button type="button" class="button subtle" data-reuse="${esc(file)}" data-task="${esc(t.id)}" data-revision="${t.revision}">用于下一轮</button>`:''}</p>`).join('')}</article>`}).join('');$('dynamic-tasks').querySelectorAll('[data-reuse]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api(path(`/runs/${run.id}/tasks/${button.dataset.task}/reuse`),{method:'POST',body:JSON.stringify({request_id:uuid(),file:button.dataset.reuse,revision:Number(button.dataset.revision),name:'已验收-'+button.dataset.reuse.split('/').at(-1)})});await loadProject();await loadCapabilities();toast('已加入下一轮素材');}catch(e){toast(e.message,true);}finally{button.disabled=false;}});}
 renderDecisions(run);const diagnosis=$('dynamic-diagnoses');const diagnosisKey=JSON.stringify(run.diagnoses||[]);if(diagnosis.dataset.signature!==diagnosisKey){diagnosis.dataset.signature=diagnosisKey;diagnosis.innerHTML=(run.diagnoses||[]).map(item=>`<details class="dynamic-decision"><summary>失败分析 · ${esc(item.task_id)}</summary><p>已知事实：${esc(item.diagnosis.facts.join('；'))}</p><p>待核实：${esc(item.diagnosis.uncertainties.join('；'))}</p><p>建议：${esc(item.diagnosis.recommendations.join('；'))}</p></details>`).join('');}const delivery=$('dynamic-delivery');const deliverySignature=JSON.stringify(run.delivery);if(delivery.dataset.signature!==deliverySignature){delivery.dataset.signature=deliverySignature;delivery.innerHTML=run.delivery.length?'<h2>正式交付</h2>'+run.delivery.map(a=>`<a class="dynamic-deliverable" href="${esc(href(a.url))}" target="_blank" rel="noopener">${esc(a.name)} · 下载 / 查看</a>`).join(''):'';}}
async function refresh(){if(stopped)return;const epoch=++refreshEpoch;try{const listing=await api(path('/runs'));if(epoch!==refreshEpoch||stopped)return;$('dynamic-history').innerHTML=listing.runs.map(w=>`<button data-run="${w.id}">${esc(w.goal.slice(0,60))}<small>${names[w.state]||esc(w.state)} · v${w.graph_version}</small></button>`).join('');document.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>{currentId=b.dataset.run;lastDecisions='';lastSnapshot=null;refresh();});if(!currentId&&listing.runs.length)currentId=listing.runs[0].id;if(currentId){const selected=currentId;const [run,events]=await Promise.all([api(path(`/runs/${selected}`)),api(path(`/runs/${selected}/events?limit=100`))]);if(epoch!==refreshEpoch||selected!==currentId||stopped)return;render(run);$('dynamic-events').innerHTML=events.events.map(e=>`<div>${esc(new Date(e.created_at*1000).toLocaleTimeString())} · ${esc(labels[e.kind]||e.kind)} · v${e.graph_version}</div>`).join('');}$('dynamic-connection').textContent=`状态已同步 · ${new Date().toLocaleTimeString()}`;}catch(e){if(epoch!==refreshEpoch)return;$('dynamic-connection').textContent='连接中断，正在重试';error(e.message);}finally{clearTimeout(timer);if(!stopped&&epoch===refreshEpoch)timer=setTimeout(refresh,2000);}}
window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);});
try{await loadProject();await loadCapabilities();await refresh();}catch(e){error(e.message);}
