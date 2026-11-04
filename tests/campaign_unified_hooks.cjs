/* Real browser interventions. A missing boundary is never a passing protocol case. */
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const key=()=>crypto.randomUUID().replaceAll('-','');
const activeJob=(run,kind)=>run.control_jobs.some(job=>job.control_kind===kind&&job.status==='running');
const terminal=run=>['SUCCEEDED','FAILED','CANCELLED'].includes(run.state);
function verifyDrainedAttempts(boundary,run,facts){
 const originals=[];
 for(const old of boundary.started||[]){
  const task=run.tasks.find(t=>t.id===old.id);assert.ok(task,'Original drained Task history must be retained');
  const matches=facts.attempts.filter(a=>a.run_id===run.id&&a.step_id===old.id&&a.task_revision===old.revision&&
   a.started<boundary.input.accepted_at&&a.finished>boundary.input.accepted_at);
  assert.equal(matches.length,1,'Input must overlap the original executing attempt');
  const original=matches[0];assert.equal(original.status,'finished','Input must not interrupt an executing attempt');
  const result=facts.business_results.find(r=>r.run_id===run.id&&r.lease_id===original.id&&r.task_revision===old.revision);
  assert.ok(result&&result.status==='committed'&&['succeeded','failed'].includes(result.outcome),'Original result must be committed normally');
  const rejected=facts.rejected_task_reviews.some(r=>r.run_id===run.id&&r.task_id===old.id&&r.revision===old.revision);
  if(result.outcome==='succeeded'&&!rejected){
   assert.equal(task.status,'succeeded');assert.equal(task.revision,old.revision);assert.equal(task.attempts,old.attempts);
  }else{
   assert.ok(task.revision>=old.revision);
   for(const next of facts.attempts.filter(a=>a.run_id===run.id&&a.step_id===old.id&&a.task_revision>old.revision))
    assert.ok(next.started>=result.committed_at,'Failure recovery cannot precede the original committed result');
  }
  originals.push(original);
 }
 return originals;
}
function selectedTask(run,identifier){
 const seen=new Set();while(!seen.has(identifier)){seen.add(identifier);const task=run.tasks.find(item=>item.id===identifier);
  if(task?.selected)return task;
  const replacement=(run.replacements||[]).find(item=>item.old_task_id===identifier);if(!replacement)return null;identifier=replacement.new_task_id;
 }throw Error('Task replacement cycle in public Run snapshot');
}
const hooks=new Set(['identity','drain_local','drain_agent','planning_inputs','drain_wan','unknown_input',
 'feedback_idempotency','no_waiver','mask_boundary','temporal_boundary','publish_input','successor_inputs','drain_sync','frozen_route',
 'feedback_cutout','feedback_layout','reference_before_wan','legal_replacement']);

async function before(c){
 const {spec,state,page,read,wait,appendInput,feedback,approve,api,fault,capture,persist,observeTerminal,control}=c;
 const hook=spec.dynamic_hook;if(!hooks.has(hook))return;
 state.unified_boundary={hook,observed:false};persist();
 const mark=(evidence={})=>{Object.assign(state.unified_boundary,{observed:true,...evidence});persist();};
 const input=async(message='保持原目标和所有验收，不重生成、不取消，继续使用已提交或已验收的结果。',targets=[])=>{
  const prior=await read();state.unified_boundary.before=prior;
  const result=await appendInput(message,[],targets);
  state.unified_boundary.input=result;state.unified_boundary.after=await read();persist();return result;
 };
 if(hook==='legal_replacement'){
  const initial=await wait(r=>r.tasks.some(t=>t.id==='render'),600),task=initial.tasks.find(t=>t.id==='render');
  if(task.spec.objective!==spec.expected_business_objective)
   throw Object.assign(Error('The planner did not preserve the declared business objective separately from fault injection'),{boundary:true});
  state.unified_boundary.business_objective=task.spec.objective;persist();
  let attempt;
  await wait(async()=>{const facts=await fault('progress');attempt=facts.attempts.find(a=>a.step_id==='render'&&a.status==='running'&&a.unit);return Boolean(attempt);},600);
  try{await fault('stop_worker',attempt.unit);await wait(r=>r.tasks.some(t=>t.id==='render'&&['failed','paused'].includes(t.status)),300);}
  finally{await fault('start_worker',attempt.unit);}
  mark({faulted_attempt:attempt.id,faulted_unit:attempt.unit});
 }
 if(hook==='drain_local'||hook==='drain_agent'){
  const run=await wait(r=>hook==='drain_local'
   ?['left','right'].every(id=>r.tasks.some(t=>t.id===id&&t.status==='running'))
   :r.tasks.some(t=>t.status==='running'&&t.type==='agent'),600);
  const started=run.tasks.filter(t=>t.status==='running').map(t=>({id:t.id,revision:t.revision,attempts:t.attempts}));
  await input();mark({started});
 }else if(hook==='planning_inputs'){
  await wait(r=>activeJob(r,'PLAN'),300);const first=await input();
  // The currently running PLAN has a frozen pre-input snapshot. Wait until the
  // safe-point PLAN freezes the first batch before submitting a later message.
  await wait(r=>r.inputs.some(i=>i.id===first.id&&i.status==='PLANNING')&&activeJob(r,'PLAN'),900);
  const second=await appendInput('补充：最终文件仍为final.png，维持64×64，不改变任何处理参数。');
  assert.ok(second.sequence>first.sequence);mark({input_ids:[first.id,second.id]});
 }else if(['drain_wan','drain_sync','frozen_route'].includes(hook)){
  let call;
  await wait(async()=>{const facts=await fault('progress');call=facts.provider_calls.find(item=>
   hook==='drain_sync'?item.state==='submitting'&&item.route_adapter==='openai_images':
    item.external_id&&['running','submitted','waiting'].includes(item.state));return Boolean(call);},600);
  if(hook!=='drain_sync')assert.equal(call.provider_identity,'wan','This case requires the actual Wan adapter');
  await input();mark({provider_before:call});
  if(hook==='frozen_route'){
   await fault('switch_provider_route');state.unified_boundary.route_switched=true;persist();
  }
 }else if(hook==='unknown_input'){
  let attempt;
  await wait(async()=>{const facts=await fault('progress');const submitted=new Set(facts.submitting.map(call=>call.attempt_id));
   attempt=facts.attempts.find(item=>item.status==='running'&&item.unit&&submitted.has(item.id));return Boolean(attempt);},600);
  state.interrupted_attempt=attempt;persist();
  try{
   await fault('stop_worker',attempt.unit);
   await wait(r=>r.tasks.some(t=>t.error_code==='submission_unknown'),300);
   const accepted=await input();
   const held=await wait(r=>r.terminal_reason==='input_waiting_external_unknown',120);
   assert.equal(held.input_barrier_sequence,accepted.sequence);assert.equal(held.delivery.length,0);
   mark({attempt});state.outcome='protocol_passed';
  }finally{await fault('start_worker',attempt.unit);}
 }else if(hook==='feedback_cutout'||hook==='feedback_layout'){
  const initial=await wait(r=>r.decisions.some(d=>d.kind==='quality'&&d.request.task_id==='render'),1200);
  state.unified_boundary.before=initial;persist();
  let edgeFeedback;
  if(hook==='feedback_cutout'){
   const task=initial.tasks.find(t=>t.id==='render');
   const inputs=Object.keys(task.spec.inputs),outputs=Object.keys(task.spec.output_roles);
   assert.equal(inputs.length,1,'Edge revision requires a single declared reference input');
   assert.equal(outputs.length,1,'Edge revision requires a single declared output');
   edgeFeedback=`给render创建新revision：沿用当前输入角色${inputs[0]}对应的已验收透明图作为唯一来源，先计算原Alpha整图的Pillow ImageFilter.MinFilter(3)结果，仅将矩形{{edge_patch_box}}（Pillow坐标，右下边界不含）内的Alpha替换为该结果；矩形外Alpha和全部RGB保持原值，其他像素处理不变；明确semantics.mask_repair为A/local_edge_repair，reference_role=${inputs[0]}，output_role=${outputs[0]}，max_changed_fraction=0.05。保持现有输入输出角色、512×512透明PNG及人物范围；按本次明确允许的局部Alpha变化更新render目标与对应内容验收，全部RGB及区外Alpha必须不变，程序和AgentReview仍须通过，不豁免失败。不重做source_cutout；保留原人工Review，待用户复核新版。`;
   state.unified_boundary.feedback_roles={reference:inputs[0],output:outputs[0]};persist();
  }
  const message=hook==='feedback_cutout'?
   edgeFeedback:
   '给render创建新revision：仅交换左右构图，prepare_b在左、prepare_a在右，仍各用ImageOps.fit默认方法适配64×64后组成128×64 final.png。两张上游revision与哈希保持，保留技术/Agent验收；保留原人工Review，待用户复核新版。';
  const accepted=await feedback(message);mark({input_id:accepted.id});
 }else if(hook==='reference_before_wan'){
  const initial=await wait(r=>r.tasks.some(t=>t.id==='reference'&&t.status==='running'),600);
  assert.equal((await fault('progress')).provider_calls.length,0);
  const accepted=await input('仅修改参考素材：reference改用已授权的rocket.png，以相同ImageOps.fit方法处理为1280×720。给reference及受影响generate创建新revision；最终目标仍为5秒缓慢推镜视频，主体按新参考图。当前reference正常结束后重规划，旧参考不得发给Wan；总生成额度仍为一次。',['reference']);
  mark({input_id:accepted.id,reference_revision:initial.tasks.find(t=>t.id==='reference').revision});
 }else if(hook==='feedback_idempotency'){
  let run=await wait(r=>r.decisions.some(d=>d.kind==='quality'),600),decision=run.decisions.find(d=>d.kind==='quality');
  await page.locator(`[data-decision="${decision.id}"] [data-feedback]`).fill('请在64×64图像内画2像素红色边框，其余不变。');
  await page.reload({waitUntil:'domcontentloaded'});
  await page.locator(`[data-decision="${decision.id}"] [data-feedback]`).waitFor();
  assert.equal(await page.locator(`[data-decision="${decision.id}"] [data-feedback]`).inputValue(),'请在64×64图像内画2像素红色边框，其余不变。');
  const accepted=await feedback('请给render创建新revision，在64×64图像内用Pillow画2像素红色边框，其余内容不变；保留原人工Review，待用户复核新版；保留技术和Agent验收。');
  const command=await api('POST',`/api/projects/${state.project}/runs/${run.id}/commands`,state.last_feedback_request);
  assert.equal(command.id,state.last_feedback_command);
  const stale=await api('POST',`/api/projects/${state.project}/runs/${run.id}/commands`,{
   request_id:key(),action:'answer',payload:{decision_id:decision.id,snapshot_digest:decision.snapshot_digest,action:'approve'}});
  run=await wait(r=>r.commands.some(x=>x.id===stale.id&&x.status==='rejected'),120);
  assert.equal(run.inputs.filter(i=>i.id===accepted.id).length,1);mark({decision_id:decision.id,input_id:accepted.id,stale_command:stale.id,draft_restored:true});
 }else if(hook==='no_waiver'){
  let run=await wait(r=>r.reviews.some(d=>d.purpose==='task'&&d.status==='rejected'),900);
  const rejectedReview=run.reviews.find(d=>d.purpose==='task'&&d.status==='rejected');
  if(run.state==='ACTIVE')await control('pause');
  run=await read();const decision=run.decisions.find(d=>['quality_feedback','input_question'].includes(d.kind));
  await page.waitForTimeout(2200);
  assert.equal(await page.locator('.dynamic-decision [data-answer=approve]').count(),0);
  const rejected=await api('POST',`/api/projects/${state.project}/runs/${run.id}/commands`,{
   request_id:key(),action:'answer',payload:{decision_id:decision?.id||rejectedReview.id,snapshot_digest:decision?.snapshot_digest||'0'.repeat(64),action:'approve'}});
  run=await wait(r=>r.commands.some(x=>x.id===rejected.id&&x.status==='rejected'),120);
  assert.equal(run.delivery.length,0);mark({decision_id:decision?.id,rejected_review:rejectedReview.id,rejected_command:rejected.id});
  state.outcome='protocol_passed';
 }else if(hook==='publish_input'||hook==='successor_inputs'){
  const original=hook==='successor_inputs'?await observeTerminal():await wait(r=>r.reviews.some(v=>v.purpose==='final'&&['pending','running','staged'].includes(v.status)),900);
  if(hook==='successor_inputs')assert.equal(original.state,'SUCCEEDED');
  const accepted=await input('请继续以portrait.png做64×64 final.png，保持原内容和技术要求；如果原版已交付，请作为新一轮确认范围后执行。');
  const repeated=await api('POST',`/api/projects/${state.project}/runs/${original.id}/inputs`,state.last_input_request);
  assert.equal(repeated.id,accepted.id);
  if(accepted.run_id!==original.id){
   const second=await api('POST',`/api/projects/${state.project}/runs/${original.id}/inputs`,{request_id:key(),content:'新一轮仍仅交付64×64 final.png，不扩展外部能力。'});
   assert.equal(second.run_id,accepted.run_id);
   const awaiting=await wait(r=>r.decisions.some(d=>d.kind==='successor_authorization'),900);
   assert.equal(awaiting.tasks.length,0);assert.equal(awaiting.scope_confirmed,0);
   await approve('approve');
  }
  mark({original_run_id:original.id,original_delivery:original.delivery,input_id:accepted.id,successor:accepted.run_id!==original.id});
 }
 await capture('unified-boundary-'+hook);persist();
}

async function after(c){
 const {spec,state,run,fault,persist}=c,hook=spec.dynamic_hook;if(!hooks.has(hook))return;
 const boundary=state.unified_boundary;
 if(['identity','mask_boundary','temporal_boundary'].includes(hook))boundary.observed=run.state==='SUCCEEDED';
 if(['drain_wan','drain_sync','frozen_route'].includes(hook)&&boundary.provider_before){
  const facts=await fault('progress'),old=boundary.provider_before,call=facts.provider_calls.find(item=>item.request_key===old.request_key);
  assert.ok(call,'Original Provider receipt must be retained');
  for(const field of ['request_key','external_id','route_adapter','provider_identity','implementation_digest']){
   if(field==='external_id'&&!old[field])continue;assert.equal(call[field],old[field],`Frozen Provider field changed: ${field}`);
  }
  assert.equal(facts.provider_calls.length,1,'Input or restart duplicated a Provider submission');boundary.provider_after=call;
 }
 if(['drain_local','drain_agent'].includes(hook)){
  const facts=await fault('progress');const originals=verifyDrainedAttempts(boundary,run,facts);
  boundary.drained_attempts=originals;
  boundary.drained_results=facts.business_results.filter(r=>originals.some(a=>a.id===r.lease_id));
  if(hook==='drain_local'){
   const left=originals.find(a=>a.step_id==='left'),right=originals.find(a=>a.step_id==='right');
   if(!left||!right||!(left.started<right.finished&&right.started<left.finished&&right.started<boundary.input.accepted_at))
    throw Object.assign(Error('The required parallel specialist/local Operation drain boundary was not reached'),{boundary:true});
   boundary.parallel_attempts=[left,right];
  }
 }
 if(['feedback_cutout','feedback_layout'].includes(hook))verifyFeedbackRevision(boundary.before,run,'render');
 if(hook==='reference_before_wan'){
  const task=selectedTask(run,'reference');assert.ok(task&&(task.revision>boundary.reference_revision||task.id!=='reference'&&task.user_revision_count>0));
  const calls=(await fault('progress')).provider_calls;assert.equal(calls.length,1);boundary.provider_after=calls[0];
 }
 if(hook==='legal_replacement'){
  const old=run.tasks.find(t=>t.id==='render'),replacement=selectedTask(run,'render'),child=run.tasks.find(t=>t.id==='package');
  assert.ok(old&&!old.selected&&old.error_code==='execution_failed'&&old.revision===0);
  assert.ok(replacement&&replacement.id==='render_fixed'&&replacement.type==='agent'&&replacement.status==='succeeded');
  assert.ok(child?.revision>0&&child.status==='succeeded');
  assert.ok(Object.values(child.spec.inputs).some(ref=>ref.step===replacement.id));
  assert.ok((run.replacements||[]).some(item=>item.old_task_id===old.id&&item.new_task_id===replacement.id));
  boundary.observed=true;boundary.replacement={old:old.id,current:replacement.id,child_revision:child.revision};
 }
 if(!boundary.observed)throw Object.assign(Error('Required unified Run boundary was not observed'),{boundary:true});
 persist();
}

function verifyFeedbackRevision(before,run,identifier){
 const original=before.tasks.find(t=>t.id===identifier),current=selectedTask(run,identifier);
 assert.equal(run.id,before.id);assert.ok(original&&current&&current.selected&&current.status==='succeeded');
 assert.ok(current.user_revision_count>(original.user_revision_count||0),'Feedback revision must retain its user input provenance');
 if(current.id===identifier)assert.ok(current.revision>original.revision);
 else{const retired=run.tasks.find(t=>t.id===identifier);assert.ok(retired&&!retired.selected);assert.equal(retired.revision,original.revision);}
 for(const task of before.tasks.filter(t=>t.id!==identifier&&t.status==='succeeded')){
  const retained=run.tasks.find(t=>t.id===task.id);assert.ok(retained);assert.equal(retained.revision,task.revision);assert.equal(retained.attempts,task.attempts);
  assert.deepEqual(retained.quality.files,task.quality.files);
 }
 return current;
}

module.exports={before,after,hooks,verifyDrainedAttempts,verifyFeedbackRevision};
