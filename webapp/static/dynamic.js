import {$,api,header,esc,href,toast,Preview,sourceItems,icon,markdown,bindMedia,openMedia} from './common.js';
import {deliveryItems,renderPlanSummary,renderRunResult,confirmationDetails,renderCandidates} from './dynamic_results.js';
import {terminalRun,acceptsFeedback,inputStatus,quotaBlockedReasons,decisionPresentation,assertCommandAccepted,commandRejectionMessage} from './dynamic_state.js';
import {chooseModel,choiceIsCurrent,configurationText,effortLabels} from './dynamic_models.js';
import {layoutDag,dagEdgePath} from './dynamic_dag.js';
import {executionLocation,executionPlace,providerExecution} from './dynamic_locations.js';
import {timingMarkup,executionTimes,runTiming,updateExecutionClocks} from './dynamic_timing.js';
import {applyText} from './text_stream.js';
import {settleTool} from './work_status.js';
const projectId=document.body.dataset.project;
const path=suffix=>`api/projects/${projectId}${suffix}`;
const names={ACTIVE:'进行中',WAITING_USER:'等待处理',CANCELLING:'正在取消',CANCELLED:'已取消',SUCCEEDED:'已完成',FAILED:'未完成',PLANNING:'正在规划',EXECUTING:'正在执行',REVIEWING:'正在审查',IDLE:'暂无执行活动',planning:'正在规划',running:'执行中',ready:'等待执行',blocked:'等待前置结果',leased:'已分配执行器',waiting:'等待外部结果',retry_wait:'等待安全重试',reviewing:'验收中',waiting_user:'等待你的确认',paused:'已暂停',cancelling:'正在取消',cancelled:'已取消',succeeded:'已完成',failed:'未完成'};
const labels={input_accepted:'补充要求已接收',input_safe_point:'当前任务已排空，开始重新规划',input_batch_applied:'补充要求已处理',manager_result_applied:'规划结果已应用',review_reused_or_blocked:'候选与证据未变，保留已有审查结论',successor_created:'已创建下一轮执行',proposal_applied:'新一轮计划已确定',planning_requested:'开始下一轮规划',proposal_rejected:'提案未通过校验，正在处理',review_requested:'开始验收',task_result_committed:'步骤结果已记录',state_changed:'工作流状态更新',command_processed:'控制请求已处理',delivered:'正式成果已交付',created:'工作流已创建'};
let project,capabilities,preview,currentId=null,current=null,newRunMode=false,timer=null,cursorTimer=null,busy=false,lastRender='',lastDecisions='',stopped=false,pendingCommand=null,uploading=0,projectLoad=0,capabilityLoad=0,refreshEpoch=0,lastSnapshot=null,lastRunSignature='',lastDagSignature='',modelChoice=null,eventStream=null,streamRun=null,liveEvents=[],agentTextEvents=[],agentStreamTurn={status:'running',text:'',text_version:[0,0],text_phase:'idle'},graphEvents=[],lockedDagNode=null,dagGraph=null,dagLayoutId='',lastAgentOutputAt=0,followConversation=true;
const completedStreams=new Set();
let dagViewportWidth=760;
let inputDraftRun=null;
// getRandomValues also works on the HTTP intranet entry, unlike randomUUID.
const uuid=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
const draftKey=(run,kind)=>`dynamic-draft:${projectId}:${run}:${kind}`;
function readDraft(run,kind){try{return JSON.parse(sessionStorage.getItem(draftKey(run,kind))||'null');}catch{return null;}}
function writeDraft(run,kind,value){try{if(value)sessionStorage.setItem(draftKey(run,kind),JSON.stringify(value));else sessionStorage.removeItem(draftKey(run,kind));}catch{/* Storage may be disabled; editing remains available. */}}
function saveInputDraft(){const form=$('dynamic-input-form');if(!form.dataset.run)return;
 writeDraft(form.dataset.run,'input',{text:$('dynamic-input-content').value,assets:[...$('dynamic-input-assets').selectedOptions].map(x=>x.value),targets:[...$('dynamic-input-targets').selectedOptions].map(x=>x.value),requestId:form.dataset.requestId});}
function restoreInputDraft(run,allowTargets=true){
 const form=$('dynamic-input-form'),switched=form.dataset.run!==run;
 if(!switched&&inputDraftRun===run)return;
 const saved=readDraft(run,'input');form.dataset.run=run;inputDraftRun=run;
 if(saved||switched){
  const draft=saved||{};$('dynamic-input-content').value=draft.text||'';
  delete form.dataset.requestId;if(draft.requestId)form.dataset.requestId=draft.requestId;
  for(const option of $('dynamic-input-assets').options)option.selected=(draft.assets||[]).includes(option.value);
  for(const option of $('dynamic-input-targets').options)option.selected=allowTargets&&(draft.targets||[]).includes(option.value);
  renderAttachmentChips();
 }
}
function error(message){$('dynamic-error').textContent=message;$('dynamic-error').hidden=!message;}
function safeName(task){return task.spec.name;}
function showArtifact(item){
 $('dynamic-preview').querySelectorAll('video,audio').forEach(media=>media.pause());
 openMedia(item);
}
function modelControls(){
 $('dynamic-model-profile').disabled=busy;
 $('dynamic-reasoning-effort').disabled=busy||!modelChoice;
 const inputBlocked=current&&quotaBlockedReasons.has(current.terminal_reason);
 $('dynamic-input-content').disabled=Boolean(inputBlocked);
 $('dynamic-input-submit').disabled=busy||uploading>0||Boolean(inputBlocked)||(!current&&!modelChoice);
 $('dynamic-upload').disabled=uploading>0||Boolean(inputBlocked);
}
function renderModelChoices(){
 const choices=capabilities.model_choices;
 modelChoice=chooseModel(choices,modelChoice||readDraft('new','model'));
 $('dynamic-model-profile').innerHTML='<option value="" disabled>请选择模型</option>'+(choices?.profiles||[]).map(p=>`<option value="${esc(p.id)}" ${p.available?'':'disabled'}>${esc(p.harness)} · ${esc(p.model)}${p.available?'':'（未配置）'}</option>`).join('');
 $('dynamic-model-profile').value=modelChoice?.profile||'';
 renderEfforts();modelControls();
 const daily=capabilities.daily_quota;$('dynamic-model-hint').textContent=(modelChoice?'沙盒 Implementer 使用本次选择；专家模型由系统配置并随本轮冻结。':'暂无已配置的沙盒模型，请选择可用模型或联系管理员。')+(daily?` 今日模型调用 ${daily.used}/${daily.limit}`:'');
}
function renderEfforts(){
 const profile=capabilities.model_choices?.profiles.find(p=>p.id===modelChoice?.profile);
 $('dynamic-reasoning-effort').innerHTML=(profile?.reasoning_efforts||[]).map(e=>`<option value="${esc(e)}">${esc(effortLabels[e]||e)}（${esc(e)}）</option>`).join('');
 $('dynamic-reasoning-effort').value=modelChoice?.reasoning_effort||'';
}
$('dynamic-model-profile').onchange=()=>{
 modelChoice=chooseModel(capabilities.model_choices,{profile:$('dynamic-model-profile').value,reasoning_effort:modelChoice?.reasoning_effort});
 renderEfforts();modelControls();writeDraft('new','model',modelChoice);
 $('dynamic-model-hint').textContent=modelChoice?'本次选择将在开始时固定，本轮执行过程中不会自动切换。':'请选择已配置的模型。';
};
$('dynamic-reasoning-effort').onchange=()=>{
 if(modelChoice){modelChoice={...modelChoice,reasoning_effort:$('dynamic-reasoning-effort').value};writeDraft('new','model',modelChoice);}
};
async function loadProject(){const epoch=++projectLoad;const value=await api(path(''));if(epoch!==projectLoad)return;
 const select=$('dynamic-input-assets'),firstLoad=!select.options.length,selected=new Set([...select.selectedOptions].map(x=>x.value));
 project=value;header('chat',project);const items=sourceItems(project);syncAssetOptions(select);
 for(const option of select.options)option.selected=selected.has(option.value)||(firstLoad&&currentId==null);
 renderAttachmentChips();if(!preview.current&&items.length)preview.show(items[0]);if(!$('dynamic-input-content').value&&!currentId)$('dynamic-input-content').value=project.draft?.prompt||'';}
async function loadCapabilities(){const epoch=++capabilityLoad;const value=await api(path('/runs/capabilities'));if(epoch!==capabilityLoad)return;
 const limits=Object.fromEntries([...document.querySelectorAll('[data-operation]')].map(input=>[input.dataset.operation,input.value]));capabilities=value;
 renderModelChoices();
 $('dynamic-provider-limits').innerHTML=capabilities.operations.filter(op=>op.external_side_effects).map(op=>`<label class="dynamic-scope-item"><span>${esc(op.id)}${op.available?'':'（未配置）'}</span><input type="number" min="0" max="10" value="${esc(limits[op.id]??(op.available?'10':'0'))}" data-operation="${esc(op.id)}" data-budget-group="${esc(op.budget_group)}" ${op.available?'':'disabled'} aria-label="${esc(op.id)} 调用上限"></label>`).join('');if(!$('dynamic-model-calls')){const input=document.createElement('input');input.id='dynamic-model-calls';input.type='number';input.min='1';input.max='10000';input.value='200';input.setAttribute('aria-label','模型调用次数上限');$('dynamic-scope').append(input);}}
function deliveryTargets(){return [...new Set([[...document.querySelectorAll('[name="delivery-target"]:checked')].map(x=>x.value),...$('dynamic-targets-other').value.split(/[\n,，]/).map(x=>x.trim()).filter(Boolean)].flat())];}
function authorization(goal){const budgets={};document.querySelectorAll('[data-operation]').forEach(input=>{const n=Number(input.value);if(n>0)budgets[input.dataset.budgetGroup]=(budgets[input.dataset.budgetGroup]||0)+n;});return {goal,allowed_assets:[...$('dynamic-input-assets').selectedOptions].map(x=>x.value),allowed_operations:[...capabilities.operations.filter(op=>!op.external_side_effects&&op.available).map(op=>op.id),...[...document.querySelectorAll('[data-operation]')].filter(x=>Number(x.value)>0).map(x=>x.dataset.operation)],allowed_agent_profiles:['editing_implementer','media_failure_diagnostician','visual_quality_reviewer'],provider_call_budget:budgets,delivery_targets:deliveryTargets(),wall_time_budget:Number($('dynamic-time').value)*60,model_token_budget:Number($('dynamic-token-budget').value),max_model_calls:Number($('dynamic-model-calls').value||200),content_fingerprint:capabilities.content_fingerprint};}
function renderAttachmentChips(){const selected=[...$('dynamic-input-assets').selectedOptions],root=$('dynamic-attachment-chips');root.hidden=!selected.length;root.innerHTML=selected.map(option=>`<span class="dynamic-attachment-chip">${icon('attach',10)} ${esc(option.textContent)}</span>`).join('');}
$('dynamic-upload').onchange=async event=>{const input=event.target,files=[...input.files];if(!files.length)return;uploading++;document.body.dataset.uploading='true';input.disabled=true;modelControls();$('dynamic-upload-label').textContent=`上传中 0/${files.length}`;const uploaded=[];
 try{for(let index=0;index<files.length;index++){const file=files[index],form=new FormData();form.append('file',file);$('dynamic-upload-label').textContent=`上传中 ${index+1}/${files.length}`;const response=await fetch(href(path('/assets')),{method:'POST',body:form,credentials:'same-origin'});if(!response.ok){let body={};try{body=await response.json();}catch{}throw new Error(body.detail||`${file.name} 上传失败`);}uploaded.push((await response.json()).id);}
  await loadProject();for(const option of $('dynamic-input-assets').options)if(uploaded.includes(option.value))option.selected=true;renderAttachmentChips();await loadCapabilities();
 }catch(e){toast(e.message,true);}finally{input.value='';uploading--;document.body.dataset.uploading=String(uploading>0);input.disabled=uploading>0;$('dynamic-upload-label').textContent='添加资料';modelControls();}};
async function command(action,payload={},requestId=uuid()){if(!current)return;const submitted=assertCommandAccepted(await api(path(`/runs/${current.id}/commands`),{method:'POST',body:JSON.stringify({request_id:requestId,action,payload})}));pendingCommand=submitted.id;toast(action==='cancel'?'停止请求已提交，正在回收沙箱和执行资源，并核实外部任务':'请求已提交，正在处理');await refresh();}
document.querySelectorAll('[data-control]').forEach(button=>button.onclick=async()=>{const action=button.dataset.control;if(action==='cancel'&&!window.confirm('确定停止这个项目当前正在执行的任务？已验收成果会保留；系统将撤销执行权限并回收沙箱、Lease 和本地资源，已提交的外部任务会继续核实到明确终态。'))return;button.disabled=true;try{await command(action);}catch(e){toast(e.message,true);}finally{button.disabled=action==='cancel'&&current?.state==='CANCELLING';}});
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
 root.innerHTML=run.decisions.map(d=>`<article class="dynamic-decision" data-decision="${d.id}"><h3>${esc(decisionPresentation(d,run).title)}</h3><p>${esc(d.request.reason||decisionPresentation(d,run).description)}</p>${d.request.question?`<p>${esc(d.request.question)}</p>`:''}${d.request.options?.length&&!['external_wait','external_submission'].includes(d.kind)?`<p>可选：${d.request.options.map(esc).join(' / ')}</p>`:''}${d.kind==='external_submission'?'<label class="field">供应商任务 ID（选择继续查询时填写）<input data-external-id maxlength="200"></label>':''}${confirmationDetails(d,project.assets)}<div class="scope-diff"></div>${acceptsFeedback(d)?`<label class="field">${d.kind==='input_question'?'你的回答':d.kind==='plan_confirmation'?'方案修改意见':d.kind==='publication_checkpoint'?'发布前修改意见':'修改意见'}<textarea data-feedback rows="3" maxlength="4000"></textarea></label><label class="field">关联素材<select data-feedback-assets multiple></select></label><label class="field">明确需要一并修改的上游步骤（可不选）<select data-upstreams multiple></select></label><button class="button primary" data-answer="revise">${d.kind==='input_question'?'发送回答':d.kind==='plan_confirmation'?'修改方案，重新确认':d.kind==='publication_checkpoint'?'按意见修改，不发布':'按意见修改并继续'}</button> `:''}${decisionPresentation(d,run).actions.map(item=>`<button class="button ${item.primary?'primary':'secondary'}" data-answer="${item.action}">${esc(item.label)}</button> `).join('')}</article>`).join('');
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
  if(['plan_change','successor_authorization','new_run'].includes(d.kind))scope.textContent=`目标：${proposed.goal}；交付目标：${proposed.delivery_targets.join("、")}；模型 Token 上限：${proposed.model_token_budget}；时限：${proposed.wall_time_budget/60} 分钟；任务/规划上限：${proposed.max_tasks}/${proposed.max_planning_rounds}；素材：${proposed.allowed_assets.map(id=>project.assets.find(asset=>asset.id===id)?.name||id).join('、')||'无'}；允许能力：${proposed.allowed_operations.join('、')}；专业 Agent：${proposed.allowed_agent_profiles.join('、')}；公网只读授权：${(proposed.allowed_network_urls||[]).join('、')||'无'}；外部调用总上限：${JSON.stringify(proposed.provider_call_budget)}；素材版本：${proposed.content_fingerprint.slice(0,12)}`;
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
    if(d.kind==='external_submission'&&payload.action==='approve')payload.external_id=element.querySelector('[data-external-id]').value.trim();
    if(payload.action==='revise'){
     payload.feedback=element.querySelector('textarea').value.trim();if(!payload.feedback)throw new Error('请填写具体修改意见。');
     payload.attachments=[...element.querySelector('[data-feedback-assets]').selectedOptions].map(option=>option.value);
     payload.upstream_tasks=[...element.querySelector('[data-upstreams]').selectedOptions].map(option=>option.value);
    }
    if(['plan_change','successor_authorization','new_run'].includes(d.kind)&&payload.action==='approve'){
     const fresh=await api(path('/runs/capabilities'));
     if(fresh.content_fingerprint!==proposed.content_fingerprint){proposed.content_fingerprint=fresh.content_fingerprint;scope.textContent+='；素材版本已更新为 '+fresh.content_fingerprint.slice(0,12)+'，请核对后再次确认。';throw new Error('素材已变化，请核对当前范围后再次确认。');}
     const {allowed_asset_digests,operation_implementations,operation_policy_digests,agent_profile_digests,...logical}=proposed;
     payload.authorization=logical;
    }
    element.dataset.requestId ||= uuid();saveFeedback();await command('answer',payload,element.dataset.requestId);
   }catch(e){toast(e.message,true);}finally{button.disabled=false;}
  });
 }
}
$('dynamic-input-form').onsubmit=async event=>{
 event.preventDefault();const content=$('dynamic-input-content').value.trim();if(!content||busy||uploading)return;busy=true;error('');modelControls();
 const origin=current?.id||null,attachments=[...$('dynamic-input-assets').selectedOptions].map(option=>option.value),targets=[...$('dynamic-input-targets').selectedOptions].map(option=>option.value);
 const input=$('dynamic-input-content');const key=$('dynamic-input-form').dataset.requestId||uuid();$('dynamic-input-form').dataset.requestId=key;saveInputDraft();$('dynamic-input-submit').disabled=true;
 try{let result;
  if(!origin){
   if(!modelChoice)throw new Error('请选择可用模型。');const chosen={...modelChoice},fresh=await api(path('/runs/capabilities'));
   if(!choiceIsCurrent(fresh.model_choices,chosen)){capabilities=fresh;renderModelChoices();throw new Error('模型配置已更新，请核对模型和推理强度后再次确认。');}
   if(fresh.content_fingerprint!==capabilities.content_fingerprint){capabilities.content_fingerprint=fresh.content_fingerprint;throw new Error('素材或工作区内容已变化，请检查当前执行范围后再次确认。');}
   result=await api(path('/runs'),{method:'POST',body:JSON.stringify({request_id:key,confirmed:true,authorization:authorization(content),parent_run_id:null,model_choice:chosen})});newRunMode=false;currentId=result.id;
  }else result=await api(path(`/runs/${origin}/inputs`),{method:'POST',body:JSON.stringify({request_id:key,content,attachments,targets})});
  if(input.value.trim()===content&&JSON.stringify([...$('dynamic-input-assets').selectedOptions].map(option=>option.value))===JSON.stringify(attachments)&&JSON.stringify([...$('dynamic-input-targets').selectedOptions].map(option=>option.value))===JSON.stringify(targets)){
   input.value='';delete $('dynamic-input-form').dataset.requestId;saveInputDraft();
  }
  if(origin){completedStreams.delete(result.run_id);if(currentId===origin)currentId=result.run_id;}followConversation=true;toast(origin?'消息已接收，正在结合已有任务处理':'正在生成方案，确认后才会执行');lastRender='';lastSnapshot=null;lastRunSignature='';lastDagSignature='';await refresh();
 }catch(e){error(e.message);toast(e.message,true);}finally{busy=false;modelControls();}
};
for(const [id,event] of [['dynamic-input-content','input'],['dynamic-input-assets','change'],['dynamic-input-targets','change']])$(id).addEventListener(event,()=>{delete $('dynamic-input-form').dataset.requestId;saveInputDraft();});
$('dynamic-input-assets').addEventListener('change',renderAttachmentChips);

// Computing and resource-waiting nodes use distinct pulse rhythms. Aggregate
// Run state, dispatch leases and review hand-offs remain visually quiet.
const activeNodeStatuses=new Set(['running']);
function resourceWaiting(item){return ['ready','retry_wait'].includes(item?.status)&&['resource','gpu_capacity'].includes(item?.wait_reason);}
function nodeState(item){const status=item.status;
 if(['succeeded','completed','applied','SUCCEEDED'].includes(status))return 'succeeded';
 if(activeNodeStatuses.has(status))return 'active';
 if(resourceWaiting(item))return 'resource-wait';
 if(['failed','rejected','cancelled','FAILED','CANCELLED'].includes(status))return 'failed';
 return 'waiting';
}
function pulseDelay(id){let value=0;for(const character of String(id))value=(value*31+character.charCodeAt(0))%1700;return -(value/1000).toFixed(3)+'s';}
function detailValue(value){if(value==null||value===''||(Array.isArray(value)&&!value.length))return '—';return typeof value==='string'?value:JSON.stringify(value,null,2);}
function renderDagDetail(node,root=$('dag-detail')){
 const item=node.item;
 if(node.kind==='run'){
  const assets=(item.authorization?.allowed_assets||[]).map(id=>project.assets.find(asset=>asset.id===id)?.name||id),inputs=item.inputs||[],deliveries=item.delivery||[],configuration=configurationText(item.model_configuration);
  root.innerHTML=`<h3>本轮任务</h3><div class="dag-detail-status">${esc(names[item.state]||item.state||'运行中')}</div><dl><dt>目标</dt><dd>${esc(detailValue(item.goal))}</dd><dt>交付要求</dt><dd>${esc(detailValue(item.authorization?.delivery_targets))}</dd><dt>当前工作</dt><dd>${esc(names[item.activity]||item.activity||'正在衔接下一步')}</dd><dt>计划</dt><dd>DAG v${esc(detailValue(item.graph_version))} · ${item.sealed?'已封口':'可继续规划'} · ${item.tasks?.filter(task=>task.selected!==false).length||0} 个业务任务</dd><dt>模型</dt><dd>${esc(configuration)}</dd><dt>授权输入</dt><dd>${esc(detailValue(assets))}</dd><dt>后续输入</dt><dd>${esc(detailValue(inputs.map(value=>({sequence:value.sequence,status:value.status,content:value.content,attachments:(value.attachments||[]).map(a=>a.name||a.id||a)}))))}</dd><dt>模型预算</dt><dd>${esc(detailValue(item.token_usage&&{remaining:item.token_usage.remaining,limit:item.token_usage.limit,known:item.token_usage.known,pending:item.token_usage.reserved_or_unknown}))}</dd><dt>创建 / 更新</dt><dd>${new Date(item.created_at*1000).toLocaleString()} / ${new Date(item.updated_at*1000).toLocaleString()}</dd></dl>${deliveries.length?`<section class="task-detail-files"><h4>正式输出</h4>${deliveries.map(file=>`<a target="_blank" rel="noopener" href="${esc(href(file.url))}">${esc(file.name)}</a>`).join('')}</section>`:''}`;return;
 }
 if(node.kind==='control'){
  root.innerHTML=`<h3>${esc(activityRole(item))}</h3><div class="dag-detail-status">${esc(names[item.status]||item.status)}</div><dl><dt>当前工作</dt><dd>${esc(activityAction(item))}</dd><dt>执行角色</dt><dd>${esc(activityRole(item))}</dd><dt>执行位置（最近尝试）</dt><dd>${esc(activityLocation(item))}</dd><dt>工具位置</dt><dd>${esc(detailValue(item.tool_location&&activityLocation({execution_location:item.tool_location})))}</dd><dt>内部类型</dt><dd>${esc(item.control_kind)} Control Job</dd><dt>阶段</dt><dd>${esc(detailValue(item.phase))}</dd><dt>计划版本</dt><dd>${esc(detailValue(item.created_graph_version))}</dd><dt>尝试次数</dt><dd>${esc(detailValue(item.attempts))}</dd><dt>错误</dt><dd class="dag-detail-error">${esc(detailValue(item.error_code))}</dd><dt>更新时间</dt><dd>${new Date(item.updated_at*1000).toLocaleString()}</dd></dl>`;return;
 }
 const spec=item.spec||{},semantics=spec.semantics||{};
 root.innerHTML=`<h3>${esc(safeName(item))}</h3><div class="dag-detail-status">${esc(names[item.status]||item.status)}</div><dl><dt>当前工作</dt><dd>${esc(activityAction(item))}</dd><dt>执行角色</dt><dd>${esc(activityRole(item))}</dd><dt>执行位置（最近尝试）</dt><dd>${esc(activityLocation(item))}</dd>${item.tool_location?`<dt>工具位置</dt><dd>${esc(activityLocation({execution_location:item.tool_location}))}</dd>`:''}<dt>目标</dt><dd>${esc(detailValue(spec.objective))}</dd><dt>执行类型</dt><dd>${esc(item.type)} · ${esc(detailValue(spec.operation||item.agent_policy?.id))}</dd><dt>生命周期</dt><dd>${esc(detailValue(item.phase))} · revision ${item.revision} · 尝试 ${item.attempts}</dd><dt>计划版本</dt><dd>${esc(detailValue(item.revision_graph_version))}</dd><dt>等待原因</dt><dd>${esc(detailValue(item.wait_reason))}</dd><dt>输入</dt><dd><code>${esc(detailValue(spec.inputs))}</code></dd><dt>输出角色</dt><dd>${esc(detailValue(spec.output_roles||spec.outputs))}</dd><dt>验收与质量</dt><dd><code>${esc(detailValue({acceptance:spec.acceptance,quality:item.quality}))}</code></dd><dt>语义约束</dt><dd><code>${esc(detailValue(semantics))}</code></dd><dt>错误</dt><dd class="dag-detail-error">${esc(detailValue(item.error_detail||item.error_code))}</dd></dl>${taskFileLinks(item)}`;
}
function taskFileLinks(task){const files=[...new Set([...(task.candidate_result||[]),...(task.accepted_result||[])])];return files.length?`<section class="task-detail-files"><h4>相关文件</h4>${files.map(file=>`<a target="_blank" rel="noopener" href="${esc(href(path(`/runs/${current.id}/tasks/${task.id}/file?file=${encodeURIComponent(file)}`)))}">${task.accepted_result.includes(file)?'已验收':'候选'} · ${esc(file.split('/').at(-1))}</a>`).join('')}</section>`:'';}
function dagStorageKey(run){return `dynamic-dag-layout:${projectId}:${run.id}:${run.graph_version}:snake-cells-v2:${dagViewportWidth}`;}
function readDagPositions(run){try{return JSON.parse(sessionStorage.getItem(dagStorageKey(run))||'{}');}catch{return {};}}
function saveDagPositions(run,graph){try{sessionStorage.setItem(dagStorageKey(run),JSON.stringify(Object.fromEntries(graph.nodes.map(node=>[node.id,[node.x,node.y]]))));}catch{/* Layout persistence is optional. */}}
function updateDagEdges(){if(!dagGraph)return;const positions=new Map();$('dag-canvas').querySelectorAll('[data-dag-node]').forEach(node=>positions.set(node.dataset.dagNode,{x:node.offsetLeft,y:node.offsetTop,width:node.offsetWidth,height:node.offsetHeight,band:dagGraph.nodes.find(item=>item.id===node.dataset.dagNode)?.band}));$('dag-canvas').querySelectorAll('[data-edge-upstream]').forEach(path=>{const upstream=positions.get(path.dataset.edgeUpstream),downstream=positions.get(path.dataset.edgeDownstream);if(upstream&&downstream)path.setAttribute('d',dagEdgePath(upstream,downstream));});}
function bindDagDragging(run,graph){const canvas=$('dag-canvas'),viewport=$('dag-viewport');
 canvas.querySelectorAll('[data-dag-node]').forEach(button=>{const node=graph.nodes.find(item=>item.id===button.dataset.dagNode);let start=null,moved=false;button.onpointerdown=event=>{if(event.button!==0)return;event.stopPropagation();start={x:event.clientX,y:event.clientY,left:button.offsetLeft,top:button.offsetTop};moved=false;button.setPointerCapture(event.pointerId);button.classList.add('dragging');};button.onpointermove=event=>{if(!start)return;const dx=event.clientX-start.x,dy=event.clientY-start.y;if(Math.hypot(dx,dy)>3)moved=true;node.x=Math.max(12,start.left+dx);node.y=Math.max(28,start.top+dy);button.style.left=node.x+'px';button.style.top=node.y+'px';canvas.style.width=Math.max(graph.width,node.x+node.width+60)+'px';canvas.style.height=Math.max(graph.height,node.y+node.height+60)+'px';updateDagEdges();};button.onpointerup=event=>{if(!start)return;button.releasePointerCapture(event.pointerId);button.classList.remove('dragging');start=null;if(moved){button.dataset.dragged='true';saveDagPositions(run,graph);setTimeout(()=>delete button.dataset.dragged,0);}};});
 let pan=null;viewport.onpointerdown=event=>{if(event.button!==0||event.target.closest('.dag-node'))return;pan={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);viewport.classList.add('panning');};viewport.onpointermove=event=>{if(!pan)return;viewport.scrollLeft=pan.left-(event.clientX-pan.x);viewport.scrollTop=pan.top-(event.clientY-pan.y);};viewport.onpointerup=event=>{if(!pan)return;viewport.releasePointerCapture(event.pointerId);pan=null;viewport.classList.remove('panning');};
}
function renderDag(run){
 if($('dag-dialog').open&&$('dag-viewport').clientWidth)dagViewportWidth=$('dag-viewport').clientWidth;
 const graph=layoutDag(run.tasks,run.dependencies,run.control_jobs,{run,planners:run.planners,reviews:run.reviews,viewportWidth:dagViewportWidth}),byId=new Map(graph.nodes.map(node=>[node.id,node]));
 const saved=readDagPositions(run);for(const node of graph.nodes)if(Array.isArray(saved[node.id])&&saved[node.id].length===2){node.x=Math.max(12,Number(saved[node.id][0])||0);node.y=Math.max(28,Number(saved[node.id][1])||0);}graph.width=Math.max(graph.width,...graph.nodes.map(node=>node.x+node.width+24));graph.height=Math.max(graph.height,...graph.nodes.map(node=>node.y+node.height+60));dagGraph=graph;dagLayoutId=dagStorageKey(run);
 const active=graph.nodes.filter(node=>node.kind!=='run'&&(activeNodeStatuses.has(node.item.status)||resourceWaiting(node.item))).length,canvas=$('dag-canvas');
 $('dag-fab-count').textContent=active||graph.nodes.length;$('dag-fab').classList.toggle('has-active',active>0);
 const signature=JSON.stringify({nodes:graph.nodes.map(node=>[node.kind,node.id,node.x,node.y,node.item.status,node.item.state,node.item.wait_reason,node.item.next_run_at,node.item.updated_at,node.item.revision,node.item.phase,node.item.execution_location,node.item.tool_location,node.item.execution_attempt_id,node.item.provider_execution_state]),attempts:run.activity_attempts,edges:graph.edges,width:graph.width,height:graph.height});
 if(signature===lastDagSignature&&canvas.childElementCount){
  if($('dag-dialog').open&&graph.nodes.length){const selected=graph.nodes.find(node=>node.id===lockedDagNode);renderDagDetail(selected||graph.nodes.find(node=>node.kind!=='run'&&(activeNodeStatuses.has(node.item.status)||resourceWaiting(node.item)))||graph.nodes[0]);}
  return;
 }
 lastDagSignature=signature;
 const edgeMarkup=graph.edges.map(edge=>{const a=byId.get(edge.upstream),b=byId.get(edge.downstream);return a&&b?`<path class="dag-edge ${edge.kind}" data-edge-upstream="${esc(edge.upstream)}" data-edge-downstream="${esc(edge.downstream)}" marker-end="url(#dag-${edge.kind}-arrow)" d="${dagEdgePath(a,b)}"></path>`:'';}).join('');
 const nodeMarkup=graph.nodes.map(node=>{const item=node.item,status=node.kind==='run'?'summary':nodeState(item),label=node.kind==='run'?'本轮任务':node.kind==='control'?activityRole(item):safeName(item),meta=node.kind==='run'?`计划版本 ${item.graph_version}`:node.kind==='control'?'控制任务':`${item.type} · revision ${item.revision}`,state=node.kind==='run'?item.state:item.status,delay=['active','resource-wait'].includes(status)?`--pulse-delay:${pulseDelay(node.id)}`:'',place=executionPlace(item,node.kind==='run'),timing=node.kind==='run'?timingMarkup(runTiming(run)):executionTimes(item,run);return `<button type="button" class="dag-node ${status} ${node.kind}" data-dag-node="${esc(node.id)}" aria-label="${esc(label+'；'+(names[state]||state||'运行中')+'；'+place.detail)}" style="left:${node.x}px;top:${node.y}px;${delay}"><span class="dag-location ${esc(place.kind)}" title="${esc(place.detail)}">${esc(place.label)}</span><strong>${esc(label)}</strong><span>${esc(resourceWaiting(item)?activityAction(item):(names[state]||state||'运行中'))}</span><small>${esc(meta)}</small>${timing}</button>`;}).join('');
 canvas.style.width=graph.width+'px';canvas.style.height=graph.height+'px';canvas.innerHTML=`${run.control_jobs.length?'<span class="dag-control-label">CONTROL FLOW</span>':''}<svg class="dag-edges" width="${graph.width}" height="${graph.height}" aria-hidden="true"><defs><marker id="dag-business-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#9fac9a"></path></marker><marker id="dag-control-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7890a8"></path></marker></defs>${edgeMarkup}</svg>${nodeMarkup}`;
 canvas.querySelectorAll('[data-dag-node]').forEach(button=>{const node=byId.get(button.dataset.dagNode);for(const event of ['pointerenter','focus'])button.addEventListener(event,()=>{if(!lockedDagNode)renderDagDetail(node);});button.addEventListener('click',()=>{if(button.dataset.dragged)return;lockedDagNode=lockedDagNode===node.id?null:node.id;canvas.querySelectorAll('.dag-node').forEach(item=>item.classList.toggle('selected',item.dataset.dagNode===lockedDagNode));renderDagDetail(node);});});bindDagDragging(run,graph);
 if($('dag-dialog').open&&graph.nodes.length){const selected=graph.nodes.find(node=>node.id===lockedDagNode);renderDagDetail(selected||graph.nodes.find(node=>activeNodeStatuses.has(node.item.status)||resourceWaiting(node.item))||graph.nodes[0]);}
}

function conversationAtBottom(){const node=$('dynamic-conversation');return node.scrollHeight-node.scrollTop-node.clientHeight<70;}
function updateConversationFollow(){followConversation=conversationAtBottom();$('dynamic-scroll-latest').hidden=followConversation;}
function scrollConversationToLatest(smooth=false){const node=$('dynamic-conversation');followConversation=true;$('dynamic-scroll-latest').hidden=true;queueMicrotask(()=>node.scrollTo({top:node.scrollHeight,behavior:smooth?'smooth':'auto'}));}
function markAgentOutput(runId,publishedAt){if(publishedAt&&Date.now()-publishedAt*1000>5000)return;lastAgentOutputAt=Date.now();clearTimeout(cursorTimer);cursorTimer=setTimeout(()=>{if(current?.id===runId)renderAgentTranscript(current);},1900);}
function renderMessages(run,additionalChanged=false){
 const root=$('dynamic-input-history'),status={ROUTING:'正在理解消息，原任务继续',REPLIED:'已回复，未改动任务',ACCEPTED:'修改已确认，等待安全处理',PLANNING:'正在重新规划',APPLIED:'已应用',SUPERSEDED:'已转入下一轮',FAILED:'消息处理失败'};
 const inputs=run.inputs||[],goalIsInput=inputs.some(item=>item.sequence===1&&item.content===run.goal&&Math.abs((item.accepted_at||0)-run.created_at)<5);
 const messages=[...(goalIsInput?[]:[{id:'goal',content:run.goal,status:'INITIAL',accepted_at:run.created_at,attachments:[]}]),...inputs];
 const signature=JSON.stringify(messages.map(item=>[item.id,item.status,item.content,item.attachments,item.accepted_at,item.applied_at,item.waiting_for_answer,item.reply,item.reply_snapshot_at,item.successor_run_id]));
 const messagesChanged=root.dataset.signature!==signature;
 if(messagesChanged){root.dataset.signature=signature;$('dynamic-user-messages').innerHTML=messages.map(item=>{const attachments=(item.attachments||[]).map(value=>typeof value==='string'?(project.assets.find(asset=>asset.id===value)?.name||value):(value.name||value.id)).filter(Boolean),state=item.waiting_for_answer?'已阅读，等待回答当前问题':item.status==='INITIAL'?'初始要求':status[item.status]||item.status,time=new Date((item.accepted_at||run.created_at)*1000).toLocaleString();return `<article class="user-message"><div class="message-author"><span>你</span><small>${esc(state)} · ${esc(time)}</small></div><div class="user-message-text">${esc(item.content)}</div>${attachments.length?`<div class="message-attachments"><span class="dynamic-message-attachments">${icon('attach',11)} ${attachments.map(esc).join('、')}</span></div>`:''}</article>${item.reply?`<section class="message-reply"><strong>任务规划师${item.reply_snapshot_at?` · 截至 ${esc(new Date(item.reply_snapshot_at*1000).toLocaleTimeString())}`:''}</strong><div>${esc(item.reply)}</div>${item.successor_run_id?`<button type="button" class="button secondary" data-message-run="${esc(item.successor_run_id)}">查看新任务方案</button>`:''}</section>`:''}`;}).join('');$('dynamic-user-messages').querySelectorAll('[data-message-run]').forEach(button=>button.onclick=()=>{currentId=button.dataset.messageRun;newRunMode=false;lastSnapshot=null;lastRunSignature='';refresh();});}
 const transcriptChanged=renderAgentTranscript(run);$('dynamic-conversation-empty').hidden=messages.length>0||!$('dynamic-agent-response').hidden;
 if(followConversation&&(messagesChanged||transcriptChanged||additionalChanged))scrollConversationToLatest();
}

function renderAgentTranscript(run){
 let changed=false;
 const actorLabel=event=>event.payload?.display_name||({manager_agent:'任务规划师',reviewer_agent:'质量审查员',diagnostician_agent:'故障诊断师',specialist_agent:'代码编辑师',sandbox_agent:'本机沙盒创作助手',remote_sandbox_agent:'远程沙盒创作助手'}[event.payload?.actor||event.payload?.execution?.actor]||'创作助手');
 const streamText=(agentStreamTurn.text||'').trim(),entries=agentTextEvents.filter(event=>typeof event.payload?.text==='string'&&event.payload.text.trim()&&!streamText.includes(event.payload.text.trim())).map(event=>({id:'note-'+(event.payload.event_id||event.seq),role:actorLabel(event),text:event.payload.text.trim(),time:event.created,attemptId:event.payload.execution?.attempt_id}));
 if(streamText){
  const roles='任务规划师|质量审查员|故障诊断师|代码编辑师|编辑实现师|本机沙盒创作助手|远程沙盒创作助手|Manager|Reviewer|Diagnostician|Implementer|Sandbox Agent',chunks=streamText.split(new RegExp(`\\n\\n(?=(?:${roles})：)`));
  let offset=(agentStreamTurn.text||'').indexOf(streamText);
  for(const [index,chunk] of chunks.entries()){
   const match=chunk.match(new RegExp(`^(${roles})：([\\s\\S]*)$`)),span=(run.agent_text_spans||[]).find(s=>s.start<=offset&&offset<s.end);
   entries.push({id:'stream-'+index,role:match?.[1]||'本机沙盒创作助手',text:(match?.[2]||chunk).trim(),attemptId:span?.attempt_id});offset+=chunk.length+2;
  }
 }
 const response=$('dynamic-agent-response'),output=$('dynamic-agent-text'),streaming=!terminalRun(run.state)&&eventStream!=null&&Date.now()-lastAgentOutputAt<1800,known=new Set(entries.map(entry=>entry.id));
 const artifacts=deliveryItems(run.delivery),artifactSignature=JSON.stringify(artifacts);
 for(const entry of entries){let node=output.querySelector(`[data-agent-output="${CSS.escape(entry.id)}"]`);if(!node){node=document.createElement('section');node.className='agent-output-entry';node.dataset.agentOutput=entry.id;node.innerHTML='<div class="agent-output-meta"><span></span><small></small></div><div class="assistant-text markdown"></div>';output.append(node);changed=true;}node.querySelector('.agent-output-meta span').textContent=entry.role;
  const attempt=(run.activity_attempts||[]).find(a=>a.id===entry.attemptId),messageAttempt=attempt&&run.control_jobs.some(job=>job.id===attempt.step_id&&job.control_kind==='MESSAGE'),timing=attempt?{...attempt,status:terminalRun(run.state)&&!messageAttempt&&attempt.finished==null?'unconfirmed':attempt.status}:null;
  node.querySelector('.agent-output-meta small').innerHTML=timing?timingMarkup(timing):esc(entry.time?new Date(entry.time*1000).toLocaleTimeString():'计时记录待同步');
  const textNode=node.querySelector('.assistant-text');if(textNode.dataset.text!==entry.text||textNode.dataset.artifacts!==artifactSignature){textNode.dataset.text=entry.text;textNode.dataset.artifacts=artifactSignature;textNode.innerHTML=markdown(entry.text,artifacts);bindMedia(textNode,artifacts,item=>showArtifact({...item,workflowDelivery:true}));changed=true;}textNode.classList.remove('streaming-text');}
 output.querySelectorAll('[data-agent-output]').forEach(node=>{if(!known.has(node.dataset.agentOutput)){node.remove();changed=true;}});const latestText=output.querySelector('[data-agent-output]:last-child .assistant-text');latestText?.classList.toggle('streaming-text',streaming&&entries.length>0);
 response.hidden=false;response.classList.toggle('streaming',streaming&&entries.length>0);
 const latestNote=agentTextEvents.at(-1);$('dynamic-agent-text-status').textContent=entries.length?`${streaming?'Agent 正在输出':'Agent 输出'}${latestNote?.created?` · ${new Date(latestNote.created*1000).toLocaleTimeString()}`:''}`:'';
 return changed;
}

function eventKindLabel(kind){return {progress:'Agent 进度',work:'执行活动',tool:'工具调用',workflow_graph:'工作流'}[kind]||labels[kind]||kind;}
function eventText(event){const payload=event.payload||{};if(typeof payload.text==='string')return payload.text;if(typeof payload.label==='string')return payload.label;if(event.kind==='tool')return payload.title||payload.command||'';if(event.kind==='workflow_graph')return labels[payload.kind]||'';return '';}
function settledActivity(event,run){const payload=event.payload||{};return settleTool(run,payload);}
function ledgerEventState(event,run){const status=event.payload?.status;if(['failed','error','cancelled'].includes(status))return 'failed';if(['completed','succeeded'].includes(status))return 'succeeded';if(settledActivity(event,run).status==='interrupted')return 'interrupted';if(terminalRun(run.state))return run.state==='SUCCEEDED'?'succeeded':'failed';return ['running','queued','started'].includes(status)?'active':'succeeded';}
function renderLedger(run,events=graphEvents){const root=$('dynamic-ledger'),nearBottom=root.scrollHeight-root.scrollTop-root.clientHeight<50,rows=[{id:'run',title:'任务已创建',detail:run.goal,time:run.created_at,state:'succeeded'}],lifecycle=new Map();
 for(const event of events){const text=labels[event.kind]||event.kind;rows.push({id:`graph-${event.seq}`,title:text,detail:`计划版本 ${event.graph_version}`,time:event.created_at,state:'succeeded'});}
 for(const event of liveEvents){if(event.kind==='workflow_graph')continue;const text=eventText(event);if(!text)continue;const payload=event.payload||{},stable=event.kind==='tool'||event.kind==='work'?`${event.kind}:${payload.execution?.attempt_id||''}:${payload.id||payload.event_id}`:`${event.kind}:${text}`;lifecycle.set(stable,{id:stable,title:text,detail:eventKindLabel(event.kind),time:event.created,state:ledgerEventState(event,run)});}
 rows.push(...lifecycle.values());rows.sort((a,b)=>a.time-b.time);const signature=JSON.stringify(rows.map(row=>[row.id,row.title,row.detail,row.time,row.state]));if(root.dataset.signature===signature)return;root.dataset.signature=signature;root.innerHTML=rows.length?rows.map(row=>`<article class="ledger-item ${esc(row.state)}"><strong>${esc(row.title)}</strong><span>${esc(row.detail)}</span><small>${new Date(row.time*1000).toLocaleTimeString()}</small></article>`).join(''):'<div class="dynamic-ledger-empty"><strong>还没有执行记录</strong><span>任务启动后会持续追加。</span></div>';
 $('dynamic-ledger-status').textContent=terminalRun(run.state)?(names[run.state]||run.state):'实时更新';if(nearBottom)queueMicrotask(()=>{root.scrollTop=root.scrollHeight;});}

function renderChatActivity(run){const bundle=$('dynamic-activity-bundle'),plan=$('dynamic-activity-plan'),tools=$('dynamic-activity-tools'),items=[...run.control_jobs,...run.tasks.filter(item=>item.selected!==false)].sort((a,b)=>a.created_at-b.created_at),lifecycle=new Map();
 if(bundle.dataset.run!==run.id){bundle.dataset.run=run.id;bundle.open=false;}
 for(const event of liveEvents){if(!['tool','work'].includes(event.kind))continue;const payload=event.payload||{},key=`${event.kind}:${payload.execution?.attempt_id||''}:${payload.id||payload.event_id}`;lifecycle.set(key,{...event,payload});}
 const signature=JSON.stringify([run.state,run.activity_attempts,items.map(item=>[item.id,item.status,item.wait_reason,item.next_run_at,item.updated_at,item.attempts]),[...lifecycle.values()].map(event=>[event.kind,event.payload.id,event.payload.status,event.payload.output])]);if(bundle.dataset.signature===signature)return;bundle.dataset.signature=signature;
 plan.innerHTML=items.length?'<div class="plan-heading">执行步骤</div>'+items.map(item=>{const state=['succeeded','completed'].includes(item.status)?'completed':['running','leased','reviewing'].includes(item.status)?'in_progress':resourceWaiting(item)?'resource_wait':item.status==='failed'?'failed':'';const retry=item.recovery_count||item.infrastructure_interruptions?` · 恢复 ${item.recovery_count||0} 次 / 中断 ${item.infrastructure_interruptions||0} 次`:'';const waiting=resourceWaiting(item)?` · ${resourceWaitAction(item)}`:item.wait_reason?' · '+waitReasonLabel(item.wait_reason):'';return `<div class="plan-step ${state}"><span>${state==='completed'?'✓':state==='failed'?'×':'•'}</span><div><strong>${esc(item.control_kind?activityRole(item):safeName(item))}</strong><small>${esc(names[item.status]||item.status)}${esc(waiting)}${item.repair_count?` · 修复 ${item.repair_count} 次`:''}${retry}</small>${executionTimes(item,run)}</div></div>`;}).join(''):'';
 tools.innerHTML=[...lifecycle.values()].map(event=>{const payload=event.payload,state=ledgerEventState(event,run),title=eventText(event)||eventKindLabel(event.kind),detail=state==='interrupted'?'执行器中断，未收到结束回执':payload.detail||payload.operation||'',output=payload.output||'';return `<details class="tool-activity" data-status="${esc(state)}"><summary><span class="tool-state-icon">${state==='succeeded'?'✓':state==='failed'?'×':state==='interrupted'?'↯':'•'}</span><div><strong class="tool-title">${esc(title)}</strong><span class="tool-target">${esc(detail)}</span></div><small class="tool-time">${new Date(event.created*1000).toLocaleTimeString()}</small></summary>${output?`<div class="tool-detail"><pre class="tool-output">${esc(output)}</pre></div>`:''}</details>`;}).join('');
 const active=items.filter(item=>['running','leased','reviewing'].includes(item.status)||resourceWaiting(item));$('dynamic-activity-summary').textContent=terminalRun(run.state)?`${items.filter(item=>item.status==='succeeded').length}/${items.length} 步已完成`:active.length?active.map(activityAction).slice(0,2).join('；'):'正在衔接下一步';$('dynamic-activity-time').innerHTML=timingMarkup(runTiming(run),'本轮历时 · ');
}

const roleActivityStatuses=new Set(['ready','running','leased','waiting','retry_wait','reviewing']);
function activityRole(item){
 if(item.display_name)return item.display_name;
 if(item.control_kind)return {PLAN:'任务规划师',MESSAGE:'任务规划师 · 对话',REVIEW:'质量审查员',DIAGNOSE:'故障诊断师'}[item.control_kind]||'流程控制助手';
 if(item.type==='agent')return item.agent_policy?.id==='editing_implementer'?'代码编辑师':'专业编辑助手';
 return '媒体执行器';
}
function activityLocation(item){
 return executionLocation(item.execution_location).label;
}
function waitReasonLabel(reason){return reason==='gpu_capacity'?'GPU 资源':reason==='resource'?'执行资源':reason||'';}
function resourceWaitAction(item){const retry=Math.max(0,Math.ceil((item.next_run_at||0)-Date.now()/1000));return `等待${waitReasonLabel(item.wait_reason)}${retry?` · ${retry} 秒后再检查`:''}`;}
function activityAction(item){
 if(item.control_kind)return {PLAN:'正在规划',REVIEW:'正在验收',DIAGNOSE:'正在诊断'}[item.control_kind]||'正在处理控制任务';
 if(resourceWaiting(item))return resourceWaitAction(item);
 if(item.status==='ready')return '等待调度';
 if(item.status==='leased')return '正在启动执行环境';
 if(item.status==='waiting')return '等待外部结果';
 if(item.status==='retry_wait')return '等待安全重试';
 if(item.status==='reviewing')return '正在衔接验收';
 const phase={local:'正在执行',sync:'正在同步执行',submit:'正在提交外部任务',poll:'正在查询外部结果',collect:'正在收取结果',validate:'正在验证结果'}[item.phase];
 return `${phase||'正在执行'} · ${item.spec?.name||item.spec?.operation||item.id}`;
}
function renderRoleActivity(run){
 const active=terminalRun(run.state)?[]:[...run.control_jobs,...run.tasks.filter(item=>item.selected!==false)].filter(item=>roleActivityStatuses.has(item.status));
 const items=active;
 const root=$('dynamic-role-activity');root.hidden=!items.length;$('dynamic-role-count').textContent=items.length?`${items.length} 项活跃工作`:'';
 const signature=JSON.stringify(items.map(item=>[item.id,item.status,item.wait_reason,item.next_run_at,item.updated_at,item.phase,item.execution_location,item.tool_location,item.execution_attempt_id,item.provider_execution_state]));if(root.dataset.signature===signature)return;root.dataset.signature=signature;
 $('dynamic-role-items').innerHTML=items.map(item=>{const role=activityRole(item),location=activityLocation(item),action=activityAction(item),state=item.status==='running'?'active':resourceWaiting(item)?'resource-wait':'waiting',tool=item.tool_location&&item.tool_location!==item.execution_location?`（工具：${executionLocation(item.tool_location).label}）`:'',external=item.provider_execution_state?` · 外部服务：${providerExecution(item.provider_execution_state)}`:'';return `<div class="dynamic-role-line ${state}"><i class="dynamic-role-pulse" aria-hidden="true"></i><span><strong>${esc(role)}</strong> 在 <b>${esc(location)}</b>${esc(tool)} ${esc(action)}${esc(external)}</span></div>`;}).join('');
}

function renderBlockingAction(run){
 const root=$('dynamic-blocking-action');if(!root)return;
 const action=run.blocking_action||(!terminalRun(run.state)&&run.control_plane?.available===false?{kind:'control_plane',code:'orchestrator_unavailable',title:'控制服务暂不可用',message:'任务和操作会在控制服务恢复后继续处理，请稍后刷新页面。'}:null);
 if(!action||(action.kind==='workflow_action'&&run.decisions.some(item=>item.id===action.decision_id))){root.innerHTML='';root.hidden=true;return;}
 root.hidden=false;
 const details=[...(action.current_targets||[]).map(value=>`当前交付：${value}`),...(action.missing_extensions||[]).map(value=>`缺少格式：${value}`),...(action.missing_names||[]).map(value=>`缺少文件：${value}`),...(action.missing_roles||[]).map(value=>`缺少角色：${value}`)];
 const examples=action.examples||[],failureDetails=(action.details||[]).map(item=>item.message||item.code).filter(Boolean);
 const guidance=action.action==='retry'?'操作：点击右上角“继续”重试失败步骤。':(examples.length?`示例：${examples.join('；')}`:'');
 root.innerHTML=`<article class="dynamic-blocking-action" role="alert"><strong>${esc(action.title||'需要你的处理')}</strong><p>${esc(action.message||'请确认下面列出的事项后继续。')}</p>${details.length?`<ul>${details.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>`:''}${failureDetails.length?`<ul>${failureDetails.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>`:''}${guidance?`<p>${esc(guidance)}</p>`:''}<small>状态：${esc(action.code||'WAITING_USER')}</small></article>`;
}

function startRunStream(runId){const sameRun=streamRun===runId;if(sameRun&&(eventStream||completedStreams.has(runId)))return;if(eventStream)eventStream.close();if(!sameRun||completedStreams.has(runId)){liveEvents=[];agentTextEvents=[];agentStreamTurn={status:'running',text:'',text_version:[0,0],text_phase:'idle'};lastAgentOutputAt=0;}completedStreams.delete(runId);streamRun=runId;const stream=new EventSource(href(path(`/runs/${runId}/stream`)));eventStream=stream;
 stream.onopen=()=>{if(stream===eventStream)$('dynamic-connection').textContent=`实时输出已连接 · ${new Date().toLocaleTimeString()}`;};
 const receiveText=(message,snapshot)=>{if(stream!==eventStream)return;let payload;try{payload=JSON.parse(message.data);}catch{return;}const previous=agentStreamTurn,next=applyText(previous,payload,snapshot);if(next?._streamGap){stream.close();eventStream=null;streamRun=null;queueMicrotask(()=>startRunStream(runId));return;}agentStreamTurn=next||previous;if(agentStreamTurn===previous)return;markAgentOutput(runId,payload.published_at);if(current?.id===runId){const shouldFollow=followConversation;renderAgentTranscript(current);if(shouldFollow)scrollConversationToLatest();}};
 stream.addEventListener('text',message=>receiveText(message,false));stream.addEventListener('text_snapshot',message=>receiveText(message,true));
 const eventIdentity=event=>event.payload?.event_id||String(event.seq);
 stream.addEventListener('activity',message=>{if(stream!==eventStream)return;let event;try{event=JSON.parse(message.data);}catch{return;}if(liveEvents.some(item=>eventIdentity(item)===eventIdentity(event)))return;liveEvents.push(event);if(liveEvents.length>160)liveEvents=liveEvents.slice(-160);if(current?.id===runId){renderLedger(current);renderChatActivity(current);}});
 stream.addEventListener('agent_text',message=>{if(stream!==eventStream)return;let event;try{event=JSON.parse(message.data);}catch{return;}if(agentTextEvents.some(item=>eventIdentity(item)===eventIdentity(event)))return;agentTextEvents.push(event);if(agentTextEvents.length>200)agentTextEvents=agentTextEvents.slice(-200);markAgentOutput(runId,event.created);if(current?.id===runId){const shouldFollow=followConversation;renderAgentTranscript(current);if(shouldFollow)scrollConversationToLatest();}});
 stream.addEventListener('done',()=>{if(stream===eventStream){completedStreams.add(runId);stream.close();eventStream=null;agentStreamTurn={...agentStreamTurn,status:'succeeded',text_phase:'idle'};if(current?.id===runId)renderAgentTranscript(current);$('dynamic-connection').textContent=`执行流已结束 · ${new Date().toLocaleTimeString()}`;refresh().catch(()=>{});}});
 stream.onerror=()=>{if(stream===eventStream)$('dynamic-connection').textContent='实时流正在重连';};
}

function renderReusable(run){
 const root=$('dynamic-reusable'),items=run.tasks.flatMap(task=>[...new Set(task.accepted_result)].map(file=>({task,file})));
 const signature=JSON.stringify(items.map(item=>[item.task.id,item.task.revision,item.file]));if(root.dataset.signature===signature)return;root.dataset.signature=signature;
 root.innerHTML=items.length?'<h2>已验收中间成果</h2><div class="reusable-list">'+items.map(({task,file})=>`<article class="reusable-item"><a target="_blank" rel="noopener" href="${esc(href(path(`/runs/${run.id}/tasks/${task.id}/file?file=${encodeURIComponent(file)}`)))}" title="${esc(file)}">${esc(file.split('/').at(-1))}</a><button type="button" class="button subtle" data-reuse="${esc(file)}" data-task="${esc(task.id)}" data-revision="${task.revision}">用于下一轮</button></article>`).join('')+'</div>':'';
 root.querySelectorAll('[data-reuse]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await api(path(`/runs/${run.id}/tasks/${button.dataset.task}/reuse`),{method:'POST',body:JSON.stringify({request_id:uuid(),file:button.dataset.reuse,revision:Number(button.dataset.revision),name:'已验收-'+button.dataset.reuse.split('/').at(-1)})});await loadProject();await loadCapabilities();toast('已加入下一轮素材');}catch(e){toast(e.message,true);}finally{button.disabled=false;}});
}

function render(run){if(lastSnapshot?.id===run.id&&(run.updated_at<lastSnapshot.updated_at||run.graph_version<lastSnapshot.graph_version))return;current=run;const snapshotSignature=JSON.stringify([run,graphEvents]);if(lastSnapshot?.id===run.id&&snapshotSignature===lastRunSignature){renderAgentTranscript(run);return;}lastRunSignature=snapshotSignature;lastSnapshot={id:run.id,updated_at:run.updated_at,graph_version:run.graph_version};const receipt=(run.commands||[]).find(c=>c.id===pendingCommand&&c.status!=='pending');if(receipt){pendingCommand=null;if(receipt.status==='applied'&&run.successor_run_id){currentId=run.successor_run_id;lastDecisions='';lastSnapshot=null;lastRunSignature='';lastDagSignature='';queueMicrotask(refresh);return;}if(receipt.status==='rejected')toast(commandRejectionMessage(receipt),true);else toast('控制请求已处理');}$('dynamic-current').dataset.runId=run.id;$('dynamic-current').dataset.status=run.state;$('dynamic-current').dataset.graphVersion=String(run.graph_version);$('dynamic-current').hidden=false;$('dynamic-empty').hidden=true;$('dynamic-run-tools').hidden=false;$('dag-fab').hidden=false;$('dynamic-title').textContent=run.goal;const cleanup=run.state==='CANCELLING'?` · 待回收 ${run.cleanup_pending} 项`:'';$('dynamic-status').textContent=`${names[run.state]||run.state} · ${names[run.activity]||run.activity}${cleanup} · 计划版本 ${run.graph_version} · ${run.sealed?'计划已封口':'根据结果继续规划'} · 模型预算剩余 ${run.token_usage.remaining}/${run.token_usage.limit} tokens${run.token_usage.reserved_or_unknown?`（${run.token_usage.reserved_or_unknown} 待核实）`:""}`;$('dynamic-updated').textContent=`最后更新 ${new Date(run.updated_at*1000).toLocaleString()}`;$('dynamic-diagnostics').href=href(path(`/runs/${run.id}/diagnostics?download=true`));
 if($('dynamic-input-form').dataset.run!==run.id)saveInputDraft();
 if(run.terminal_notice)$('dynamic-status').textContent+=` · ${run.terminal_notice}`;else if(run.recovery_notices?.length)$('dynamic-status').textContent+=` · 已自动恢复 ${run.recovery_notices.length} 类异常`;
 if(!terminalRun(run.state)&&run.control_plane?.available===false)$('dynamic-status').textContent+=' · 控制服务暂不可用，恢复后继续处理';
 $('dynamic-model-current').textContent=configurationText(run.model_configuration);const targetsKey=JSON.stringify([run.id,run.authorization_version,run.authorization.delivery_targets]);if($('dynamic-targets-other').dataset.confirmed!==targetsKey){const targets=run.authorization.delivery_targets||[],known=[...document.querySelectorAll('[name=delivery-target]')];known.forEach(input=>input.checked=targets.includes(input.value));$('dynamic-targets-other').value=targets.filter(value=>!known.some(input=>input.value===value)).join('，');$('dynamic-targets-other').dataset.confirmed=targetsKey;}
 const active=!terminalRun(run.state);$('dynamic-workspace-title').textContent='任务对话';$('dynamic-execution-settings').hidden=true;$('dynamic-context-settings').hidden=false;$('dynamic-input-content').placeholder=active?'继续描述修改意见或下一步要求…':'描述下一轮修改要求…';$('dynamic-input-submit').setAttribute('aria-label',active?'发送修改要求':'开始下一轮修改');
 document.querySelector('[data-control=pause]').hidden=!active||['WAITING_USER','CANCELLING'].includes(run.state);
 const inputBlocked=quotaBlockedReasons.has(run.terminal_reason),retryLabel=run.blocking_action?.retry_options?.[0]?.label||'重试失败步骤';document.querySelector('[data-control=resume]').hidden=run.state!=='WAITING_USER'||inputBlocked||run.blocking_action?.action==='replan';document.querySelector('[data-control=resume]').title=run.blocking_action?.action==='retry'?retryLabel:'继续';document.querySelector('[data-control=resume]').setAttribute('aria-label',run.blocking_action?.action==='retry'?retryLabel:'继续');const stopProject=document.querySelector('[data-control=cancel]');stopProject.hidden=!active;stopProject.disabled=run.state==='CANCELLING';stopProject.textContent=run.state==='CANCELLING'?'■ 正在停止':'■ 停止项目';
 modelControls();
 $('dynamic-input-form').hidden=false;syncAssetOptions($('dynamic-input-assets'));renderAttachmentChips();
 $('dynamic-input-status').textContent=inputStatus(run);
 const choices=$('dynamic-input-targets');$('dynamic-input-target-field').hidden=terminalRun(run.state)||!run.tasks.some(item=>item.selected);
 if(terminalRun(run.state))for(const option of choices.options)option.selected=false;
 const known=new Map([...choices.options].map(option=>[option.value,option]));
 for(const task of run.tasks.filter(item=>item.selected)){if(!known.has(task.id)){const option=document.createElement('option');option.value=task.id;option.textContent=task.spec.name;choices.append(option);}known.delete(task.id);}
 for(const option of known.values())option.remove();
 restoreInputDraft(run.id,!terminalRun(run.state));
 startRunStream(run.id);const planChanged=renderPlanSummary($('dynamic-plan-summary'),run),resultChanged=renderRunResult($('dynamic-run-result'),run,showArtifact);renderMessages(run,planChanged||resultChanged);renderCandidates($('dynamic-candidates'),run,showArtifact);renderBlockingAction(run);renderRoleActivity(run);renderLedger(run);renderChatActivity(run);
 $('dynamic-control-jobs').innerHTML=run.control_jobs.map(job=>`<p>${{PLAN:'规划',REVIEW:'审查',DIAGNOSE:'诊断'}[job.control_kind]} · ${names[job.status]||esc(job.status)}${job.error_code?' · '+esc(job.error_code):''}</p>`).join('');

 const signature=JSON.stringify(run.tasks);if(signature!==lastRender){lastRender=signature;$('dynamic-tasks').innerHTML=run.tasks.map(t=>{const semantics=t.spec.semantics||{};const semanticText=[semantics.subject?.mode,semantics.consistency_mode,semantics.mask_repair?`蒙版 ${semantics.mask_repair.level}`:'',...(semantics.temporal||[]).map(x=>`${x.action} · ${x.transition}`)].filter(Boolean).join(' · ');return `<article class="dynamic-task" data-status="${esc(t.status)}"><header><strong>${esc(safeName(t))}</strong><span>${t.selected?(names[t.status]||esc(t.status)):'已由新步骤替代（保留历史）'}</span></header><p>${esc(t.spec.objective||'')}</p><small>${esc(t.type)} · 修订 ${t.revision} · 调度 ${t.attempts}${t.wait_reason?' · '+esc(t.wait_reason):''}</small>${semanticText?`<p class="muted">${esc(semanticText)}</p>`:''}${t.error_code?`<p role="status">${esc(t.error_detail||t.error_code)}</p>`:''}${[...new Set([...t.candidate_result,...t.accepted_result])].map(file=>`<p><a target="_blank" rel="noopener" href="${esc(href(path(`/runs/${run.id}/tasks/${t.id}/file?file=${encodeURIComponent(file)}`)))}">${t.accepted_result.includes(file)?'已验收':'待验收'} · ${esc(file.split('/').at(-1))}</a></p>`).join('')}</article>`}).join('');}
 renderDecisions(run);renderReusable(run);const diagnosis=$('dynamic-diagnoses');const diagnosisKey=JSON.stringify(run.diagnoses||[]);if(diagnosis.dataset.signature!==diagnosisKey){diagnosis.dataset.signature=diagnosisKey;diagnosis.innerHTML=(run.diagnoses||[]).map(item=>`<details class="dynamic-decision"><summary>失败分析 · ${esc(item.task_id)}</summary><p>已知事实：${esc(item.diagnosis.facts.join('；'))}</p><p>待核实：${esc(item.diagnosis.uncertainties.join('；'))}</p><p>建议：${esc(item.diagnosis.recommendations.join('；'))}</p></details>`).join('');}const delivery=$('dynamic-delivery');const deliverySignature=JSON.stringify([run.id,run.delivery]);if(delivery.dataset.signature!==deliverySignature){const firstDelivery=delivery.dataset.run!==run.id||delivery.dataset.hasDelivery!=='true';delivery.dataset.signature=deliverySignature;delivery.dataset.run=run.id;delivery.dataset.hasDelivery=String(run.delivery.length>0);const items=deliveryItems(run.delivery);delivery.innerHTML=items.length?'<h2>正式交付</h2><div class="delivery-list">'+items.map((item,index)=>`<article class="dynamic-deliverable"><button type="button" data-delivery="${index}" aria-label="预览 ${esc(item.name)}">${icon(item.type.startsWith('video/')?'play':item.type.startsWith('image/')?'image':'file',16)}</button><span><strong title="${esc(item.name)}">${esc(item.name)}</strong><small>${esc(item.subtitle)}</small></span><a href="${esc(href(item.url))}" download title="下载 ${esc(item.name)}" aria-label="下载 ${esc(item.name)}">${icon('download',15)}</a></article>`).join('')+'</div>':'';delivery.querySelectorAll('[data-delivery]').forEach(button=>button.onclick=()=>showArtifact(items[Number(button.dataset.delivery)]));if(items.length&&(firstDelivery||!preview.current))preview.show({...items.find(item=>item.is_primary)||items[0],workflowDelivery:true});}renderDag(run);}
async function refresh(){
 if(stopped)return;const epoch=++refreshEpoch;
 try{
  const listing=await api(path('/runs'));if(epoch!==refreshEpoch||stopped)return;
  $('dynamic-run-count').textContent=listing.runs.length?`${listing.runs.length} 次`:'暂无';
  if(!currentId&&!newRunMode&&listing.runs.length)currentId=listing.runs[0].id;
  $('dynamic-history').innerHTML=listing.runs.map(w=>`<button data-run="${w.id}" class="${w.id===currentId?'selected':''}"><span>${esc(w.goal.slice(0,60))}</span><small>${names[w.state]||esc(w.state)} · DAG v${w.graph_version}</small></button>`).join('')||'<p class="empty-small">还没有任务</p>';
  $('dynamic-history').querySelectorAll('[data-run]').forEach(button=>button.onclick=async()=>{newRunMode=false;currentId=button.dataset.run;followConversation=true;$('dynamic-scroll-latest').hidden=true;lastDecisions='';lastSnapshot=null;lastRunSignature='';lastDagSignature='';await refresh();if(current?.id===currentId)$('dag-fab').click();});
  if(currentId){
   const selected=currentId;const [run,events]=await Promise.all([api(path(`/runs/${selected}`)),api(path(`/runs/${selected}/events?limit=100`))]);
   if(epoch!==refreshEpoch||selected!==currentId||stopped)return;graphEvents=events.events;render(run);
   $('dynamic-events').innerHTML=events.events.map(e=>`<div>${esc(new Date(e.created_at*1000).toLocaleTimeString())} · ${esc(labels[e.kind]||e.kind)} · v${e.graph_version}</div>`).join('');
  }else{current=null;if(eventStream)eventStream.close();eventStream=null;streamRun=null;liveEvents=[];agentTextEvents=[];agentStreamTurn={status:'running',text:'',text_version:[0,0],text_phase:'idle'};lastAgentOutputAt=0;clearTimeout(cursorTimer);followConversation=true;$('dynamic-scroll-latest').hidden=true;graphEvents=[];lastRunSignature='';lastDagSignature='';$('dynamic-current').hidden=true;$('dynamic-empty').hidden=false;$('dynamic-run-tools').hidden=false;$('dag-fab').hidden=true;$('dynamic-execution-settings').hidden=false;$('dynamic-context-settings').hidden=false;$('dynamic-input-target-field').hidden=true;restoreInputDraft('new',false);$('dynamic-input-content').placeholder='描述你要完成的任务…';$('dynamic-input-submit').setAttribute('aria-label','提交并生成方案');$('dynamic-input-status').textContent='';$('dynamic-user-messages').innerHTML='';for(const id of ['dynamic-plan-summary','dynamic-run-result']){$(id).hidden=true;$(id).innerHTML='';delete $(id).dataset.signature;}$('dynamic-input-history').dataset.signature='';$('dynamic-agent-text').textContent='';$('dynamic-agent-text-status').textContent='';$('dynamic-agent-response').hidden=true;$('dynamic-conversation-empty').hidden=false;$('dynamic-ledger').innerHTML='<div class="dynamic-ledger-empty"><strong>还没有执行记录</strong><span>任务启动后会持续追加。</span></div>';$('dynamic-ledger-status').textContent='等待任务';}
  error('');if(!eventStream)$('dynamic-connection').textContent=current&&terminalRun(current.state)?`执行已结束 · ${new Date(current.updated_at*1000).toLocaleTimeString()}`:`状态已同步 · ${new Date().toLocaleTimeString()}`;
 }catch(e){if(epoch!==refreshEpoch)return;$('dynamic-connection').textContent='连接中断，正在重试';error(e.message);}
 finally{if(epoch===refreshEpoch){clearTimeout(timer);if(!stopped&&!(current&&terminalRun(current.state)&&!current.message_pending))timer=setTimeout(refresh,2000);}}
}
const executionClockTimer=setInterval(()=>{if(!document.hidden)updateExecutionClocks();},1000);
window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);clearTimeout(cursorTimer);clearInterval(executionClockTimer);eventStream?.close();});
$('dynamic-new-run').onclick=async()=>{saveInputDraft();document.querySelectorAll('[name=delivery-target]').forEach(input=>input.checked=false);$('dynamic-targets-other').value='';delete $('dynamic-targets-other').dataset.confirmed;newRunMode=true;currentId=null;current=null;lastDecisions='';lastSnapshot=null;lastRunSignature='';lastDagSignature='';await refresh();$('dynamic-input-content').focus();};
$('dynamic-conversation').addEventListener('scroll',updateConversationFollow,{passive:true});$('dynamic-scroll-latest').onclick=()=>scrollConversationToLatest(true);
$('dag-fab').querySelector('.dag-fab-core').innerHTML=icon('spark',21);
$('dag-reset').innerHTML=icon('undo',16);$('dag-reset').onclick=()=>{if(!current)return;try{sessionStorage.removeItem(dagStorageKey(current));}catch{}lastDagSignature='';renderDag(current);$('dag-viewport').scrollTo({left:0,top:0,behavior:'smooth'});};
$('dag-fab').onclick=()=>{if(!current)return;$('dag-dialog').showModal();renderDag(current);renderDagDetail({kind:'run',item:current});const first=$('dag-canvas').querySelector('.dag-node.active,.dag-node');first?.scrollIntoView({block:'center',inline:'center'});};
$('dag-close').onclick=()=>{lockedDagNode=null;$('dag-dialog').close();};
$('dag-dialog').addEventListener('click',event=>{if(event.target===$('dag-dialog'))$('dag-dialog').close();});
const dagResizeObserver=new ResizeObserver(()=>{if(current&&$('dag-dialog').open&&$('dag-viewport').clientWidth!==dagViewportWidth)renderDag(current);});
dagResizeObserver.observe($('dag-viewport'));
window.addEventListener('pagehide',()=>dagResizeObserver.disconnect());
preview=new Preview($('dynamic-preview'));
try{await loadProject();await loadCapabilities();await refresh();}catch(e){error(e.message);}
