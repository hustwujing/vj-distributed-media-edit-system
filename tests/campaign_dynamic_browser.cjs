/* Live browser campaign for the isolated dynamic deployment. No mocked results. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execute=promisify(execFile),assert=require('node:assert/strict');
const unified=require('./campaign_unified_hooks.cjs');
const {selectOptional}=require('./campaign_browser_inputs.cjs');
const REPO=path.resolve(__dirname,'..'),PY=path.join(REPO,'.venv/bin/python');
const DEPLOY_ROOT=path.resolve(process.env.DYNAMIC_CASE_DEPLOY_ROOT||path.join(REPO,'.cluster'));
const ROOT=path.join(DEPLOY_ROOT,'campaigns/dynamic-plan');
const BASE=(process.env.DYNAMIC_CASE_BASE||'http://127.0.0.1/distributed-agent-system').replace(/\/$/,'');
const MODEL_PROFILE=process.env.DYNAMIC_MODEL_PROFILE||'claude';
const EXPECT_MODEL=process.env.DYNAMIC_EXPECT_MODEL||'claude-4.6-sonnet';
const MODEL_EFFORT=process.env.DYNAMIC_MODEL_EFFORT||'high';
const TOKEN_BUDGET_MULTIPLIER=Math.max(1,Math.min(4,Number(process.env.DYNAMIC_TOKEN_BUDGET_MULTIPLIER||1)));
const baseURL=new URL(BASE);
assert.equal(baseURL.pathname,process.env.DYNAMIC_EXPECT_PATH||'/distributed-agent-system','Refusing a main/unknown application path');
const manifest=JSON.parse(fs.readFileSync(path.join(REPO,'docs/real-case-campaign/dynamic-cases.json')));
const requested=process.argv[2]&&process.argv[2]!=='all'?new Set(process.argv[2].split(',')):null;
const cases=requested?[...requested].map(id=>manifest.cases.find(c=>c.id===id)).filter(Boolean):manifest.cases;
if(requested)assert.equal(cases.length,requested.size,'Unknown case ID');
const save=(file,value)=>{fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});fs.writeFileSync(file,JSON.stringify(value,null,2)+'\n',{mode:0o600});};
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const UNIFIED_SOURCE=fs.readFileSync(path.join(__dirname,'campaign_unified_hooks.cjs'));
const INPUT_SOURCE=fs.readFileSync(path.join(__dirname,'campaign_browser_inputs.cjs'));
const DRIVER_SOURCE=fs.readFileSync(__filename),DRIVER_SHA=crypto.createHash('sha256').update(DRIVER_SOURCE).digest('hex');
const uid=()=>crypto.randomUUID().replaceAll('-','');
const currentRelease=()=>JSON.parse(fs.readFileSync(path.join(DEPLOY_ROOT,'release.json')));
const startupRelease=currentRelease();assert.equal(startupRelease.state,'ready','Campaign requires a ready deployment');assert.match(startupRelease.config_sha256,/^[a-f0-9]{64}$/,'Campaign requires a frozen configuration digest');
const terminal=s=>['SUCCEEDED','FAILED','CANCELLED'].includes(s);
const paused=run=>run.state==='WAITING_USER'&&!run.decisions.length;
const deciding=run=>run.state==='WAITING_USER'&&run.decisions.length>0;
const controlActive=(run,kind)=>run.control_jobs.some(job=>job.control_kind===kind&&['ready','running','leased'].includes(job.status));
const globalHooks=new Set(['orchestrator_restart','orchestrator_competition','executor_restart','worker_restart','unsealed_hold','artifact_integrity','submission_unknown','observer_restart','api_restart','deployment_isolation','legal_replacement',
 'manager_hard_kill','reviewer_hard_kill','agent_hard_kill','repeated_agent_hard_kill','provider_poll_hard_kill']);
// Capacity-sensitive parallel-drain coverage also needs an otherwise idle cluster.
for(const hook of ['unknown_input','frozen_route','drain_local'])globalHooks.add(hook);
const pauseNewCases=()=>fs.existsSync(path.join(ROOT,'pause-new-cases'));
let interrupted=false,secondInterrupt=false,stopPromise=Promise.resolve();const activeStops=new Set();
const interrupt=signal=>{
 if(interrupted){if(secondInterrupt)process.exit(130);secondInterrupt=true;return;}
 interrupted=true;fs.writeFileSync(path.join(ROOT,'pause-new-cases'),`Campaign interrupted by ${signal}; active cases are being cancelled and cleaned\n`);
 stopPromise=Promise.allSettled([...activeStops].map(stop=>stop(signal)));
};
process.on('SIGINT',()=>interrupt('SIGINT'));process.on('SIGTERM',()=>interrupt('SIGTERM'));

function targetText(spec){
 if(spec.delivery_target_text)return spec.delivery_target_text;
 const e=spec.expect;
 if(e.kind==='image')return `符合需求的 ${e.width}×${e.height} 图片${e.min_images>1?'及全部要求的图片版本':''}${e.extra_extension?'，包含 '+e.extra_extension+' 分层文件':''}`;
 if(e.kind==='video')return `符合需求的完整 ${e.duration} 秒 ${e.width}×${e.height} 视频及要求的附加文件`;
 if(e.kind==='video_variants')return '符合需求的所有画幅视频及要求的附加文件';
 if(e.kind==='audio')return `符合需求的 ${e.duration} 秒音频及要求的格式版本`;
 if(e.kind==='text')return '符合需求的 '+e.extensions.join(' 和 ')+' 文件';
 return '符合本次要求的成果与可核实的处理记录';
}

async function runCase(browser,spec){
 const parent=path.join(ROOT,'runs',spec.id);fs.mkdirSync(parent,{recursive:true,mode:0o700});
 const number=1+Math.max(0,...fs.readdirSync(parent).filter(x=>/^attempt-\d+$/.test(x)).map(x=>Number(x.slice(8))));
 const directory=path.join(parent,'attempt-'+String(number).padStart(3,'0'));fs.mkdirSync(directory,{mode:0o700});fs.mkdirSync(path.join(directory,'output'));
 const state={case:spec.id,title:spec.title,run_id:null,campaign_attempt_id:new Date().toISOString().replace(/[-:.]/g,'')+'-'+uid().slice(0,8),
  status:'running',started_at:Date.now()/1000,base_url:BASE,release:currentRelease(),driver_sha256:DRIVER_SHA,
  requests:[],events:[],page_errors:[],console_errors:[],browser_assertions:[],artifacts:[],task_ids:[],run_ids:[],faults:[]};
 assert.equal(state.release.namespace,process.env.DYNAMIC_EXPECT_NAMESPACE||'dynamic-agent');
 save(path.join(directory,'case-definition.json'),spec);fs.writeFileSync(path.join(directory,'driver-snapshot.cjs'),DRIVER_SOURCE);
 const harness=path.join(directory,'harness');fs.mkdirSync(harness,{mode:0o700});state.helper_sha256={};
 for(const name of ['campaign_dynamic_probe.py','campaign_dynamic_faults.py','campaign_probe.py','campaign_archive.py','campaign_complex_checks.py','campaign_dynamic_intervention_checks.py','campaign_unified_hooks.cjs','campaign_browser_inputs.cjs','campaign_unified_checks.py','campaign_feedback_preview.py']){if(name==='campaign_unified_hooks.cjs')fs.writeFileSync(path.join(harness,name),UNIFIED_SOURCE);else if(name==='campaign_browser_inputs.cjs')fs.writeFileSync(path.join(harness,name),INPUT_SOURCE);else fs.copyFileSync(path.join(REPO,'tests',name),path.join(harness,name));state.helper_sha256[name]=sha(path.join(harness,name));}
 state.evidence_versioning='Driver captured once at process start; helper code frozen per attempt';
 state.fixture_hashes=Object.fromEntries(spec.assets.map(name=>[name,sha(path.join(ROOT,'fixtures',name))]));
 const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
 page.setDefaultTimeout(45000);let run=null,stoppedWorker=null,orchestratorStopped=false,observerStopped=false,apiStopped=false,workersHeld=false,secondStarted=false,objectCorrupted=false,lastCapture='';
 const persist=()=>save(path.join(directory,'state.json'),state);
 const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const requestRecord=response=>{const url=new URL(response.url());if(url.origin===baseURL.origin&&url.pathname.includes('/api/'))state.requests.push({at:Date.now()/1000,method:response.request().method(),path:url.pathname,status:response.status(),request_id:response.headers()['x-request-id']||null,serving_api:response.headers()['x-video-use-api']||null});};
 page.on('response',requestRecord);page.on('pageerror',e=>state.page_errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error')state.console_errors.push(m.text().slice(0,1000));});
 const storedApi=async(method,suffix,data)=>{const storage=JSON.parse(fs.readFileSync(path.join(directory,'browser-state.json')));const cookie=storage.cookies.map(item=>`${item.name}=${item.value}`).join('; ');
  if(!cookie)throw Error('Saved browser session is unavailable for interrupt recovery');const response=await fetch(BASE+suffix,{method,headers:{cookie,...(data===undefined?{}:{'content-type':'application/json'})},...(data===undefined?{}:{body:JSON.stringify(data)})});
  state.requests.push({at:Date.now()/1000,method,path:suffix,status:response.status,request_id:response.headers.get('x-request-id')||null});const body=await response.json().catch(()=>({}));if(!response.ok)throw Error(`HTTP ${response.status} ${suffix}: ${JSON.stringify(body)}`);return body;};
 const api=async(method,suffix,data)=>{if(interrupted)return storedApi(method,suffix,data);const deadline=Date.now()+60000;while(true){const response=await context.request.fetch(BASE+suffix,{method,...(data===undefined?{}:{data}),timeout:Math.max(1000,Math.min(45000,deadline-Date.now()))});
  state.requests.push({at:Date.now()/1000,method,path:suffix,status:response.status(),request_id:response.headers()['x-request-id']||null});
  const body=await response.json().catch(()=>({}));const databaseUnavailable=response.status()===503&&body.code==='API_DATABASE_UNAVAILABLE';
  if(method==='GET'&&databaseUnavailable&&Date.now()+1500<deadline){state.events.push({action:'database_read_retry',path:suffix,request_id:body.request_id,at:Date.now()/1000});persist();await delay(1500);continue;}
  if(!response.ok())throw Object.assign(Error(`HTTP ${response.status()} ${suffix}: ${JSON.stringify(body)}`),{dependency:databaseUnavailable});return body;}};
 const read=async()=>api('GET',`/api/projects/${state.project}/runs/${state.run_id}`);
 const helperEnvironment={...process.env,DYNAMIC_CAMPAIGN_REPO:REPO};
 const probe=async(action)=>{for(let attempt=1;attempt<=3;attempt++){persist();try{const {stdout}=await execute(PY,[path.join(harness,'campaign_dynamic_probe.py'),action,directory],{cwd:REPO,env:helperEnvironment,timeout:600000,maxBuffer:8*1024*1024});return JSON.parse(stdout.trim().split('\n').at(-1));}
  catch(error){if(!['inspect','final'].includes(action)||attempt===3||!String(error.stderr||error.message).includes('psycopg.OperationalError'))throw error;
   save(path.join(directory,`audit-retry-${action}-${attempt}.json`),{at:Date.now()/1000,error:String(error.stderr||error.message).slice(-12000)});
   state.events.push({action:'database_audit_retry',audit_action:action,attempt,at:Date.now()/1000});persist();await page.waitForTimeout(1500);}}};
 const fault=async(action,unit,attempt)=>{persist();const args=[path.join(harness,'campaign_dynamic_faults.py'),action,directory];if(unit)args.push('--unit',unit);if(attempt)args.push('--attempt',attempt);
  const {stdout}=await execute(PY,args,{cwd:REPO,env:helperEnvironment,timeout:180000,maxBuffer:4*1024*1024});const value=JSON.parse(stdout.trim().split('\n').at(-1));
  if(action!=='progress'){state.faults.push(value);persist();}return value;};
 const wait=async(predicate,seconds=300)=>{const until=Date.now()+seconds*1000;let value,pausedAt=0;while(Date.now()<until){value=await read();if(await predicate(value))return value;
  const diagnosing=controlActive(value,'DIAGNOSE');
  if(paused(value)&&!diagnosing&&!unified.hasActiveWork(value)){pausedAt||=Date.now();}else pausedAt=0;
  if(terminal(value.state)||(pausedAt&&Date.now()-pausedAt>5000)){save(path.join(directory,'boundary-not-reached.json'),value);throw Object.assign(Error('Workflow stopped before the required test boundary: '+value.state),{boundary:true});}
  await page.waitForTimeout(750);}save(path.join(directory,'wait-timeout.json'),value);throw Object.assign(Error('Timed out waiting for the required workflow boundary'),{boundary:true});};
 const capture=async(label)=>{await page.screenshot({path:path.join(directory,label+'.png'),fullPage:true});};
 const commandApplied=async id=>{const end=Date.now()+90000;while(Date.now()<end){const value=await read(),receipt=value.commands.find(c=>c.id===id);if(receipt&&receipt.status!=='pending'){assert.equal(receipt.status,'applied','Control command rejected: '+receipt.error_code);state.events.push({action:'command_applied',command_id:id,at:Date.now()/1000});persist();return value;}await page.waitForTimeout(500);}throw Error('Control command was not processed before the deadline');};
 const control=async(action)=>{await page.locator(`[data-control="${action}"]`).waitFor({state:'visible'});let confirmation=null;if(action==='cancel')page.once('dialog',async dialog=>{confirmation={type:dialog.type(),message:dialog.message()};await dialog.accept();});const response=page.waitForResponse(r=>r.url().endsWith('/commands')&&r.request().method()==='POST');await page.locator(`[data-control="${action}"]`).click();const result=await response;assert.equal(result.status(),202,await result.text());if(action==='cancel'){assert.equal(confirmation?.type,'confirm');state.browser_assertions.push('Cancel required an explicit browser confirmation');}state.events.push({action,at:Date.now()/1000});persist();await commandApplied((await result.json()).id);const updated=await read();if(updated.successor_run_id){state.run_id=updated.successor_run_id;state.execute_id=state.run_id;state.run_ids.push(state.run_id);state.task_ids.push(state.run_id);persist();}};
 const start=async(prompt=spec.prompt,targets=targetText(spec),budget=Math.min(4000000,
  (spec.complexity==='simple'?500000:4000000)*TOKEN_BUDGET_MULTIPLIER))=>{
  await page.locator('#dynamic-input-form').waitFor({state:'visible'});await page.locator('#dynamic-input-content').fill(prompt);
  const origin=await page.locator('#dynamic-input-form').getAttribute('data-run'),creating=origin==='new';
  if(creating){await page.locator('#dynamic-execution-settings').evaluate(node=>{node.open=true;});await page.locator('[name="delivery-target"]').evaluateAll(nodes=>nodes.forEach(node=>{node.checked=false;node.dispatchEvent(new Event('change',{bubbles:true}));}));await page.locator('#dynamic-targets-other').fill(targets);}
  await page.locator('#dynamic-model-profile').selectOption(MODEL_PROFILE);
  await page.locator('#dynamic-reasoning-effort').selectOption(MODEL_EFFORT);
  const modelCapabilities=await api('GET',`/api/projects/${state.project}/runs/capabilities`);
  assert.equal(modelCapabilities.model_protocol_policy,'plan_draft_v1_strict_preferred');
  state.model_protocol_policy=modelCapabilities.model_protocol_policy;
  await page.locator('#dynamic-scope').evaluate(node=>{node.open=true;});await page.locator('#dynamic-token-budget').fill(String(spec.dynamic_hook==='token_budget'?1024:budget));
  for(const operation of spec.allowed_external_operations||[]){const input=page.locator(`[data-operation="${operation}"]`);if(await input.isDisabled())throw Object.assign(Error('Required provider is not configured: '+operation),{dependency:true});await input.fill('1');}
  await capture('authorization-'+state.run_ids.length);
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&(creating?r.url().endsWith(`/api/projects/${state.project}/runs`):r.url().endsWith(`/runs/${origin}/inputs`)),{timeout:60000});
  await page.locator('#dynamic-input-submit').click();
  let result=await Promise.race([response,page.locator('#dynamic-error').filter({hasText:'变化'}).waitFor({state:'visible',timeout:60000}).then(()=>null)]);
  if(!result){await capture('authorization-content-refresh-'+state.run_ids.length);state.events.push({action:'reconfirm_refreshed_content',at:Date.now()/1000});await page.locator('#dynamic-input-submit').click();result=await response;}
  assert.equal(result.status(),202,await result.text());const submitted=await result.json();run=creating?submitted:await api('GET',`/api/projects/${state.project}/runs/${submitted.run_id}`);
  assert.equal(run.model_configuration.model,EXPECT_MODEL);
  assert.equal(run.model_configuration.reasoning_effort,MODEL_EFFORT);
  assert.equal(run.model_configuration.roles.editing_implementer.model,EXPECT_MODEL);
  assert.equal(run.model_configuration.roles.editing_implementer.reasoning_effort,MODEL_EFFORT);
  (state.model_configurations??=[]).push({run_id:run.id,...run.model_configuration});
  state.run_id=run.id;state.execute_id=run.id;state.run_ids.push(run.id);state.task_ids.push(run.id);persist();
  (state.authorizations??=[]).push({run_id:run.id,goal:run.goal,delivery_targets:run.authorization.delivery_targets,
   allowed_assets:run.authorization.allowed_assets,provider_call_budget:run.authorization.provider_call_budget,
   model_token_budget:run.authorization.model_token_budget});persist();
  for(const operation of spec.allowed_external_operations||[])assert.equal(run.authorization.provider_call_budget[operation],1,'The confirmed provider limit was lost: '+operation);
  if(state.upload_race)assert.equal(run.authorization.provider_call_budget[state.upload_race.operation],1,'Delayed refresh consent was not persisted');
  await context.storageState({path:path.join(directory,'browser-state.json')});fs.chmodSync(path.join(directory,'browser-state.json'),0o600);
  console.log(JSON.stringify({case:spec.id,event:'started',workflow:run.id,task:run.id}));return run;};
 const approve=async(action,decisionId=null)=>{const button=page.locator(`${decisionId?'[data-decision="'+decisionId+'"]':'.dynamic-decision'} [data-answer="${action}"]`).first();await button.waitFor({state:'visible'});await capture('decision-'+state.events.length);
  const response=page.waitForResponse(r=>r.url().endsWith('/commands')&&r.request().method()==='POST');await button.click();
  let result=await Promise.race([response,page.locator('.dynamic-decision .scope-diff').filter({hasText:'请核对后再次确认'}).waitFor({state:'visible'}).then(()=>null)]);
  if(!result){await capture('scope-fingerprint-reconfirmation');await button.click();result=await response;}
  assert.equal(result.status(),202);state.events.push({action:'decision_'+action,at:Date.now()/1000});persist();await commandApplied((await result.json()).id);const updated=await read();if(updated.successor_run_id){state.run_id=updated.successor_run_id;state.execute_id=state.run_id;state.run_ids.push(state.run_id);state.task_ids.push(state.run_id);persist();}};
 const reuse=async(task)=>{const file=task.quality.output_roles?.primary_image||task.quality.output_roles?.video||task.accepted_result[0];
  const button=page.locator(`[data-reuse="${file}"][data-task="${task.id}"]`).first();await button.waitFor({state:'visible'});
  const response=page.waitForResponse(r=>r.url().endsWith('/reuse')&&r.request().method()==='POST');await button.click();const result=await response;assert.equal(result.status(),201,await result.text());const asset=await result.json();
  (state.reuse_links??=[]).push({source_workflow:state.run_id,task_id:task.id,revision:task.revision,asset_id:asset.id,sha256:asset.metadata.provenance.sha256});persist();return asset.id;};
 const appendInput=async(content,attachments=[],targets=[])=>{
  const origin=state.run_id;await page.locator('#dynamic-input-content').fill(content);
  const settings=page.locator('#dynamic-context-settings'),assets=page.locator('#dynamic-input-assets');await settings.waitFor({state:'visible'});if(!await settings.evaluate(node=>node.open))await settings.locator('summary').click();
  if(!await assets.isVisible())await settings.evaluate(node=>{node.open=true;});await assets.scrollIntoViewIfNeeded();await assets.waitFor({state:'visible'});
  await assets.evaluate((node,values)=>{const selected=new Set(values);for(const option of node.options)option.selected=selected.has(option.value);node.dispatchEvent(new Event('change',{bubbles:true}));},attachments);
  await selectOptional(page.locator('#dynamic-input-targets'),targets);
  const response=page.waitForResponse(r=>r.url().endsWith(`/runs/${origin}/inputs`)&&r.request().method()==='POST');
  await capture('input-before-'+state.events.length);await page.locator('#dynamic-input-submit').click();
  const result=await response;assert.equal(result.status(),202,await result.text());const input=await result.json();state.last_input_request=result.request().postDataJSON();
  (state.inputs??=[]).push(input);state.events.push({action:'input_accepted',input_id:input.id,sequence:input.sequence,run_id:input.run_id,at:Date.now()/1000});
  if(input.run_id!==state.run_id){state.run_id=input.run_id;state.execute_id=input.run_id;state.run_ids.push(input.run_id);state.task_ids.push(input.run_id);}
  persist();return input;
 };
 const cancelForInterrupt=async signal=>{
  state.interrupted_by=signal;state.events.push({action:'campaign_interrupt',signal,at:Date.now()/1000});persist();
  if(!state.project)return;
  try{const listing=await storedApi('GET',`/api/projects/${state.project}/runs`);for(const item of listing.runs)if(!terminal(item.state))await storedApi('POST',`/api/projects/${state.project}/runs/${item.id}/commands`,{request_id:uid(),action:'cancel'});}
  catch(error){state.interrupt_cancel_error=error.message;persist();}
 };
 activeStops.add(cancelForInterrupt);
 const feedback=async(content,attachments=[])=>{
  run=await read();const decision=run.decisions.find(item=>['quality','quality_feedback','input_question'].includes(item.kind));assert.ok(decision,'A feedback decision must exist');
  if(!state.feedback_baseline&&decision.request.task_id&&['R11','R15','D28','D36','D37'].includes(spec.id)){state.feedback_baseline=await downloadCandidate(run,decision,'feedback-original.png');persist();}
  if(content.includes('{{edge_patch_box}}')){
   assert.ok(state.feedback_baseline,'Local edge feedback requires the original candidate');
   const original=path.join(directory,state.feedback_baseline.file);
   const result=await execute(PY,[path.join(harness,'campaign_feedback_preview.py'),original,original,'alpha_erode_patch'],{cwd:REPO,env:helperEnvironment,timeout:60000,maxBuffer:1024*1024});
   const patch=JSON.parse(result.stdout.trim());assert.ok(patch.expected_changed_pixels>0&&patch.expected_changed_fraction<=.01);
   state.feedback_patch=patch;content=content.replaceAll('{{edge_patch_box}}',JSON.stringify(patch.patch_box));persist();
  }
  const card=page.locator(`[data-decision="${decision.id}"]`);await card.locator('[data-feedback]').fill(content);
  await selectOptional(card.locator('[data-feedback-assets]'),attachments);
  const response=page.waitForResponse(r=>r.url().endsWith('/commands')&&r.request().method()==='POST');
  await capture('feedback-before-'+state.events.length);await card.locator('[data-answer=revise]').click();const result=await response;
  assert.equal(result.status(),202,await result.text());const command=await result.json();state.last_feedback_request=result.request().postDataJSON();state.last_feedback_command=command.id;await commandApplied(command.id);
  const updated=await read(),input=updated.inputs.find(item=>item.context.decision_id===decision.id);assert.ok(input,'Feedback must enter the common Run inbox');
  (state.inputs??=[]).push(input);state.events.push({action:'feedback_accepted',decision_id:decision.id,input_id:input.id,sequence:input.sequence,at:Date.now()/1000});persist();return input;
 };
 async function downloadCandidate(snapshot,decision,name,forApproval=false){
  const task=snapshot.tasks.find(item=>item.id===decision.request.task_id);assert.ok(task&&task.revision===decision.request.revision);
  assert.ok(task.quality.technical_valid,'Technical failure cannot be approved');
  if(forApproval){assert.ok(task.spec.acceptance.some(rule=>rule.kind==='review'),'Only a declared user Review can be approved');
   assert.ok(!task.quality.requires_agent||task.quality.agent_review_passed,'Agent refusal cannot be approved');}
  const file=task.candidate_result.find(value=>spec.manual_review==='video'?/\.(mp4|mov|mkv|webm)$/i.test(value):value.endsWith('.png'));
  assert.ok(file,`A declared ${spec.manual_review==='video'?'video':'PNG'} candidate is required`);
  const response=await context.request.get(BASE+`/api/projects/${state.project}/runs/${snapshot.id}/tasks/${task.id}/file?file=${encodeURIComponent(file)}`);
  assert.equal(response.status(),200);const data=await response.body();assert.ok(data.length<=20*1024*1024);
  const target=path.join(directory,name);fs.writeFileSync(target,data,{mode:0o600});const digest=sha(target);
  assert.equal(digest,task.quality.files.find(item=>item.name===file).sha256);
  return {run_id:snapshot.id,task_id:task.id,revision:task.revision,path:file,file:name,sha256:digest};
 }
 async function reviewRevisedCandidate(snapshot,decision){
  const transforms={R11:'border_4',R15:'unchanged',D28:'border_2',D36:'alpha_erode_patch',D37:'swap_halves'};
  if(!transforms[spec.id]||!state.feedback_baseline)return false;
  const candidate=await downloadCandidate(snapshot,decision,'feedback-reviewed-'+decision.id+'.png',true);
  const result=await execute(PY,[path.join(harness,'campaign_feedback_preview.py'),path.join(directory,state.feedback_baseline.file),path.join(directory,candidate.file),transforms[spec.id]],{cwd:REPO,env:helperEnvironment,timeout:60000,maxBuffer:1024*1024});
  const checked=JSON.parse(result.stdout.trim());save(path.join(directory,'feedback-review-'+decision.id+'.json'),{...checked,decision_id:decision.id,snapshot_digest:decision.snapshot_digest});
  if(!checked.passed)throw Object.assign(Error('Revised candidate failed the independent user-review pixel check; no approval was submitted'),{model:true});
  state.browser_assertions.push('Revised candidate passed independent pixel comparison before the declared user Review was approved');persist();
  await approve('approve',decision.id);return true;
 }
 const recordSources=base=>{
  state.revision_baseline=base.tasks.map(task=>({id:task.id,revision:task.revision,attempts:task.attempts,selected:task.selected,status:task.status,quality:task.quality}));
  state.checkpoint_sources=base.tasks.filter(task=>task.selected&&task.status==='succeeded'&&['prepare_a','prepare_b','clip'].includes(task.id)).map(task=>{
   const file=task.quality.output_roles?.primary_image||task.quality.output_roles?.video||task.accepted_result[0];
   const record=task.quality.files.find(item=>item.name===file);assert.ok(record?.sha256);
   return {run_id:base.id,task_id:task.id,revision:task.revision,path:file,sha256:record.sha256};});persist();
 };
 async function observeTerminal(){
  const end=Date.now()+Number(process.env.DYNAMIC_CASE_TIMEOUT_SECONDS||10800)*1000;let resumed=0;
  while(Date.now()<end){run=await read();state.last_status=run.state;
   const signature=JSON.stringify([run.state,run.graph_version,run.tasks.map(t=>[t.id,t.status])]);
   if(signature!==lastCapture){lastCapture=signature;save(path.join(directory,'workflow-latest.json'),run);persist();if(state.events.filter(e=>e.action==='snapshot').length<40){await capture('stage-'+String(state.events.length).padStart(3,'0'));state.events.push({action:'snapshot',status:run.state,graph_version:run.graph_version,at:Date.now()/1000});}}
   if(terminal(run.state))return run;
   if(deciding(run)){
    const quality=run.decisions.find(d=>d.kind==='quality');
    if(quality){
     if(await reviewRevisedCandidate(run,quality)){await page.waitForTimeout(1000);continue;}
     if(spec.manual_review==='video'){
      const candidate=await downloadCandidate(run,quality,'manual-review-candidate.mp4',true);
      const sheet=path.join(directory,'manual-review-contact-sheet.jpg');
      await execute('ffmpeg',['-y','-loglevel','error','-i',path.join(directory,candidate.file),'-vf','fps=1,scale=320:180,tile=5x1','-frames:v','1',sheet],{timeout:120000,maxBuffer:1024*1024});
      state.pending_manual_review={decision_id:quality.id,candidate,contact_sheet:path.basename(sheet)};persist();
      const verdictFile=path.join(directory,'manual-review-decision.json'),deadline=Date.now()+900000;
      while(Date.now()<deadline&&!fs.existsSync(verdictFile))await page.waitForTimeout(1000);
      assert.ok(fs.existsSync(verdictFile),'Declared user Review did not receive an independent decision');
      const verdict=JSON.parse(fs.readFileSync(verdictFile));assert.ok(['approve','reject'].includes(verdict.action));
      state.manual_review_decision=verdict;state.browser_assertions.push('Declared video Review used a downloaded hash-verified candidate and retained contact sheet before the decision');persist();
      await approve(verdict.action,quality.id);await page.waitForTimeout(1000);continue;
     }
     state.pending_quality=quality.id;persist();throw Object.assign(Error('Unplanned quality decision requires independent content review'),{model:true});
    }
    const decision=run.decisions[0];state.pending_decision={id:decision.id,kind:decision.kind,reason:decision.request.reason};persist();
    throw Object.assign(Error(['plan_change','successor_authorization','new_run'].includes(decision.kind)?
     'Unplanned authorization expansion was correctly paused':'Workflow requested user input or feedback after bounded processing: '+decision.kind),{model:true});
   }
   if(paused(run)){
    if(controlActive(run,'DIAGNOSE')){await page.waitForTimeout(1000);continue;}
    const recoverable=run.tasks.some(t=>t.status==='paused'&&['poll','collect'].includes(t.phase)&&['poll_failed','collect_failed'].includes(t.error_code));
    if(recoverable&&resumed<1){resumed++;await control('resume');await page.waitForTimeout(2500);continue;}
    return run;
   }
   await page.waitForTimeout(1000);
  }
  throw Object.assign(Error('Observation deadline reached; existing task was not resubmitted'),{observation:true});
 }
 try{
  await page.goto(BASE+'/',{waitUntil:'domcontentloaded'});await page.locator('#new-project').click();await page.locator('#new-project-form input[name="name"]').fill('DYNAMIC-QA-'+state.campaign_attempt_id+'-'+spec.id);
  await page.locator('#new-project-form .mode-choices label').filter({has:page.locator('input[value="chat"]')}).click();await page.locator('#new-project-form button[type="submit"]').click();await page.waitForURL(/\/chat\/[a-f0-9]{32}/);
  state.project=await page.locator('body').getAttribute('data-project');assert.match(state.project,/^[a-f0-9]{32}$/);persist();
  state.browser_context=await page.evaluate(()=>({secure:isSecureContext,randomUUID:typeof crypto.randomUUID,getRandomValues:typeof crypto.getRandomValues}));
  await page.waitForFunction(()=>document.querySelector('#dynamic-connection').textContent.includes('同步'));
  if(spec.assets.length){await page.locator('#dynamic-upload').setInputFiles(spec.assets.map(n=>path.join(ROOT,'fixtures',n)));await page.waitForFunction(n=>document.querySelectorAll('#dynamic-input-assets option').length===n&&document.body.dataset.uploading!=='true',spec.assets.length);}
  await page.waitForFunction(()=>document.querySelector('#dynamic-connection').textContent.includes('同步'));
  if(spec.dynamic_hook==='token_budget'){
   await page.locator('#dynamic-execution-settings').evaluate(node=>{node.open=true;});
   await page.locator('#dynamic-scope').evaluate(node=>{node.open=true;});
   const firstAsset=page.locator('#dynamic-input-assets option').first(),firstId=await firstAsset.getAttribute('value');await firstAsset.evaluate(node=>{node.selected=false;node.parentElement.dispatchEvent(new Event('change',{bubbles:true}));});
   const limit=page.locator('[data-operation]:not([disabled])').first(),operation=await limit.getAttribute('data-operation');
   let releaseResponse,markHeld;const held=new Promise(resolve=>{markHeld=resolve;}),released=new Promise(resolve=>{releaseResponse=resolve;});
   const pattern=`**/api/projects/${state.project}/runs/capabilities`;
   await page.route(pattern,async route=>{const response=await route.fetch();markHeld();await released;await route.fulfill({response});},{times:1});
   try{
    await page.locator('#dynamic-upload').setInputFiles(path.join(ROOT,'fixtures/rocket.png'));await held;
    assert.equal(await page.locator('#dynamic-input-submit').isDisabled(),true,'Submit was enabled before upload refresh completed');
    await limit.fill('1');assert.equal(await page.locator(`#dynamic-input-assets option[value="${firstId}"]`).isChecked(),false);
    await capture('upload-response-held');releaseResponse();
    await page.waitForFunction(()=>document.body.dataset.uploading==='false');
    assert.equal(await page.locator(`[data-operation="${operation}"]`).inputValue(),'1','Late capabilities response overwrote consent');
    assert.equal(await page.locator(`#dynamic-input-assets option[value="${firstId}"]`).isChecked(),false,'Asset refresh overwrote selection');
    await page.locator(`#dynamic-input-assets option[value="${firstId}"]`).evaluate(node=>{node.selected=true;node.parentElement.dispatchEvent(new Event('change',{bubbles:true}));});
    state.upload_race={operation,limit:1,real_response_delayed:true,submit_disabled:true,selection_preserved:true};
    state.browser_assertions.push('Delayed real capabilities response preserved the edited provider limit and asset selection; upload blocked submit');persist();
   }finally{releaseResponse();await page.unroute(pattern);}
  }
  await capture('initial');
  if(spec.dynamic_hook==='stale_authorization'){
   await page.locator('#dynamic-input-content').fill(spec.prompt);await page.locator('#dynamic-execution-settings').evaluate(node=>{node.open=true;});await page.locator('[name="delivery-target"]').evaluateAll(nodes=>nodes.forEach(node=>{node.checked=false;node.dispatchEvent(new Event('change',{bubbles:true}));}));await page.locator('#dynamic-targets-other').fill(targetText(spec));
   const second=await context.newPage();await second.goto(BASE+'/chat/'+state.project);await second.locator('#dynamic-upload').setInputFiles(path.join(ROOT,'fixtures/rocket.png'));await second.waitForFunction(()=>document.querySelectorAll('#dynamic-input-assets option').length===2);await second.close();
   await page.locator('#dynamic-input-submit').click();await page.locator('#dynamic-error').filter({hasText:'变化'}).waitFor();
   assert.equal((await api('GET',`/api/projects/${state.project}/runs`)).runs.length,0);state.outcome='protocol_passed';state.browser_checked=true;state.browser_assertions.push('Stale content did not create a workflow');await capture('stale-authorization');
  }else{
   const intervention=spec.id.startsWith('R');
   let initial=spec.prompt;
   const gated=['R01','R02','R03','R11','R15','R17'].includes(spec.id);
   const initialVideo=['R07','R08','R09'].includes(spec.id);
   if(intervention){
    initial='先对 portrait.png 与 rocket.png 分别使用 image.local_adjust 做 brightness=1.1 调整（parameters 内嵌 parameters），保存两个独立可复用的已验收节点 prepare_a 和 prepare_b。这两个中间节点只校验原图尺寸及文件有效性，不限制 alpha 通道是否存在：完全不透明的 RGBA 也是正确结果。';
    if(spec.assets.includes('clip_a.mp4'))initial+='另建 clip 节点，使用 media.edit 将 clip_a.mp4 前4秒处理成128×64、24fps、保留音频的MP4，保留该节点供后续复用。';
    if(['R04','R05','R06'].includes(spec.id))initial+='在两个图片节点后插入 staging 本地脚本节点，等待30秒再生成 staging.json；export 必须依赖 staging。';
    initial+=initialVideo?'export 本地脚本节点把已验收 clip 导出为4秒128×64 MP4，并将 prepare_a 使用 Pillow ImageOps.fit 默认方法制作128×64 cover.png。':'export 本地脚本节点将两张已处理图片用 Pillow ImageOps.fit 默认方法分别适配到64×64后并排合成128×64 final.png。';
    initial+='export 声明 retry_policy=safe_once，正式处理前等待20秒。';
    if(spec.id==='R14')initial+='export 写完 final.png 后，打印一行 JSON {"type":"step_progress","text":"QA_EXPORT_OUTPUT_READY"} 并 flush，再等待30秒才结束。';
    initial+=gated?'export 必须声明 review 人工验收，等待我决定。':'最后通过程序和最终验收交付。';
    if(spec.id==='R17')initial='先用 image.local_adjust 将 portrait.png 亮度调为1.1，保存 prepare_a 可复用成果；再生成64×48 PNG，必须声明 review 人工验收；待我确认后才交付。';
   }
   await start(initial,intervention?(initialVideo?'4秒128×64 MP4及128×64封面PNG':spec.id==='R17'?'64×48 PNG':'128×64双图PNG'):targetText(spec));
   const hook=spec.dynamic_hook;
   await unified.before({spec,state,page,read,wait,appendInput,feedback,approve,api,fault,capture,persist,observeTerminal,control});run=await read();
   if(hook==='cancel_planning'){await wait(w=>w.control_jobs.some(t=>t.control_kind==='PLAN'&&t.status==='running'),90);await control('cancel');run=await wait(w=>terminal(w.state));assert.equal(run.state,'CANCELLED');state.outcome='protocol_passed';}
   else if(hook==='token_budget'){run=await wait(w=>paused(w),120);assert.equal(run.terminal_reason,'model_token_budget_exhausted');state.outcome='protocol_passed';state.browser_assertions.push('Token budget stopped before another model request');}
   else if(hook==='authorize_provider'||hook==='reject_provider'){
    run=await wait(w=>deciding(w),240);assert.ok(run.decisions.some(d=>d.kind==='plan_change'));
    await approve(hook==='authorize_provider'?'approve':'reject');
    if(hook==='reject_provider'){run=await wait(w=>paused(w));state.outcome='protocol_passed';}
   }
   else if(hook==='orchestrator_restart'){
    await wait(w=>w.control_jobs.some(t=>t.control_kind==='PLAN'&&t.status==='running'),90);await fault('stop_orchestrator');orchestratorStopped=true;
    const until=Date.now()+240000;let staged=false;while(Date.now()<until){if((await fault('progress')).staged_results){staged=true;break;}await page.waitForTimeout(1000);}assert.ok(staged,'Worker did not persist a result while controller was down');
    await capture('controller-down');await fault('start_orchestrator');orchestratorStopped=false;
   }
   else if(hook==='orchestrator_competition'){await fault('start_second_orchestrator');secondStarted=true;await page.waitForTimeout(2000);assert.ok((await fault('progress')).orchestrators.length>=2);}
   else if(['manager_hard_kill','reviewer_hard_kill','agent_hard_kill','repeated_agent_hard_kill','provider_poll_hard_kill'].includes(hook)){
    const interruptions=hook==='repeated_agent_hard_kill'?2:1,killed=[];
    for(let round=0;round<interruptions;round++){
     const until=Date.now()+600000;let selected;
     while(Date.now()<until){const observed=await fault('progress');assert.ok(Object.values(observed.worker_services).every(Boolean),'All dedicated workers must be healthy before hard-kill injection');const calls=new Map(observed.provider_calls.map(call=>[call.step,call]));selected=observed.attempts.find(item=>item.status==='running'&&item.unit&&(
      hook==='manager_hard_kill'?item.control_kind==='PLAN':hook==='reviewer_hard_kill'?item.control_kind==='REVIEW':
      hook==='provider_poll_hard_kill'?!item.control_kind&&calls.get(item.step_id)?.external_id:
      !item.control_kind&&item.type==='agent')&&(hook==='provider_poll_hard_kill'||Date.now()/1000-item.started>1));if(selected)break;await page.waitForTimeout(250);}
     assert.ok(selected,`No ${hook} attempt reached the hard-kill boundary`);stoppedWorker=selected.unit;
     try{await fault('kill_attempt_worker',selected.unit,selected.id);}catch(error){stoppedWorker=null;if(hook==='provider_poll_hard_kill'&&String(error.message).includes('No replay-safe QA task')){round--;continue;}throw error;}
     killed.push(selected);await capture(`${hook}-killed-${round+1}`);
     const takeoverUntil=Date.now()+300000;let takeover;
     while(Date.now()<takeoverUntil){const observed=await fault('progress');takeover=observed.attempts.find(item=>item.step_id===selected.step_id&&item.generation>selected.generation&&(round===interruptions-1||item.status==='running'));if(takeover)break;await page.waitForTimeout(750);}
     assert.ok(takeover,`No takeover followed ${hook}`);await fault('start_worker',selected.unit);stoppedWorker=null;
    }
    const settleUntil=Date.now()+120000;let reservations=[];while(Date.now()<settleUntil){const observed=await fault('progress');reservations=observed.model_reservations.filter(item=>killed.some(attempt=>attempt.id===item.attempt_id)&&item.state==='reserved');if(!reservations.length)break;await page.waitForTimeout(750);}
    assert.equal(reservations.length,0,'Interrupted model reservations did not converge');state.hard_kill_attempts=killed;state.browser_assertions.push(`${interruptions} hard-killed attempt(s) were fenced and recovered`);persist();
   }
   else if(hook==='observer_restart'){await fault('stop_observer');observerStopped=true;}
   else if(hook==='api_restart'){apiStopped=true;await fault('stop_api');const boundary=Date.now()/1000;await page.reload({waitUntil:'domcontentloaded'});await page.locator('#dynamic-current').waitFor({state:'visible'});
    await page.waitForTimeout(2000);assert.ok(state.requests.some(r=>r.at>=boundary&&r.status===200&&r.serving_api?.includes('19767')),'The second API did not actually serve the reloaded workflow');
    state.browser_assertions.push('API a was stopped; the reloaded browser was served by API b');}
   else if(hook==='deployment_isolation'){const result=await fault('isolation');assert.ok(result.main_database_read_denied&&result.main_config_unchanged&&result.separate_encryption_key&&result.separate_object_prefix&&result.main_processes_unchanged_during_check);assert.ok(result.shared_db_dependency_independent&&result.dependency_lifecycle_probe.consumer_survived_dependency_stop&&result.dependency_lifecycle_probe.consumer_pid_unchanged&&result.dependency_lifecycle_probe.provider_recovered);}
   else if(hook==='foreign_project'){
    const outsider=await browser.newContext();for(const endpoint of [`/api/projects/${state.project}/runs/${run.id}`,`/api/projects/${state.project}/runs/${run.id}/events`])assert.equal((await outsider.request.get(BASE+endpoint)).status(),404);
    assert.equal((await outsider.request.post(BASE+`/api/projects/${state.project}/runs/${run.id}/commands`,{data:{request_id:uid(),action:'cancel'}})).status(),404);await outsider.close();state.browser_assertions.push('Foreign browser could not read or cancel the workflow');
   }
   if(hook==='pause_resume'){
    const active=await wait(w=>w.tasks.some(t=>t.status==='running'),300),activeId=active.tasks.find(t=>t.status==='running').id;
    await control('pause');run=await wait(w=>paused(w));const pauseAt=Date.now()/1000;await capture('paused');
    const deadline=Date.now()+120000;while(Date.now()<deadline){run=await read();if(run.tasks.find(t=>t.id===activeId).status==='succeeded')break;await page.waitForTimeout(750);}
    assert.equal(run.state,'WAITING_USER');assert.equal(run.tasks.find(t=>t.id===activeId).status,'succeeded','The active task did not finish while paused');await page.waitForTimeout(2500);
    const observed=await fault('progress');assert.ok(observed.attempts.every(a=>a.started<=pauseAt+.1));state.pause_boundary={completed_task:activeId,paused_at:pauseAt,attempts:observed.attempts};state.browser_assertions.push('The in-flight task completed while paused; its downstream did not start until resume');persist();await capture('upstream-complete-while-paused');await control('resume');
   }
   if(hook==='executor_restart'||hook==='worker_restart'||hook==='submission_unknown'){
    const until=Date.now()+360000;let selected;
    while(Date.now()<until){const observed=await fault('progress');selected=observed.attempts.find(a=>!a.control_kind&&a.status==='running'&&a.unit&&(hook==='submission_unknown'?observed.submitting.some(p=>p.attempt_id===a.id):a.retry_safe&&Date.now()/1000-a.started>2));if(selected)break;await page.waitForTimeout(750);}
    assert.ok(selected,'No eligible execution boundary was reached');stoppedWorker=selected.unit;state.interrupted_attempt=selected;await fault('stop_worker',stoppedWorker);await capture('worker-stopped');
    if(hook==='submission_unknown'){run=await wait(w=>paused(w),240);assert.ok(run.tasks.some(t=>t.error_code==='submission_unknown'));state.outcome='protocol_passed';}
    else{const end=Date.now()+240000;let taken=false;while(Date.now()<end){const observed=await fault('progress');if(observed.attempts.some(a=>a.step_id===selected.step_id&&a.generation>selected.generation&&a.node!==selected.node)){taken=true;break;}await page.waitForTimeout(1000);}assert.ok(taken,'No cross-node takeover was observed');state.browser_assertions.push('A different node recovered the interrupted task');}
    await fault('start_worker',stoppedWorker);stoppedWorker=null;
   }
   if(hook==='unsealed_hold'){
    run=await wait(w=>!w.sealed&&w.tasks.some(t=>t.status==='succeeded'),360);await fault('stop_orchestrator');orchestratorStopped=true;
    await page.waitForTimeout(2000);run=await read();assert.equal(run.delivered_at,null);assert.equal(run.delivery.length,0);await capture('unsealed-no-delivery');
    await fault('start_orchestrator');orchestratorStopped=false;
   }
   if(hook==='artifact_integrity'){
    await wait(w=>w.tasks.some(t=>t.status==='running'),300);await fault('stop_orchestrator');orchestratorStopped=true;
    const end=Date.now()+240000;let staged=false;while(Date.now()<end){const p=await fault('progress');if(p.staged_business_success&&p.quiescent){staged=true;break;}await page.waitForTimeout(750);}assert.ok(staged,'No completed, staged business output was observed');
    workersHeld=true;await fault('drain_workers');await fault('start_orchestrator');orchestratorStopped=false;
    run=await wait(w=>!w.sealed&&w.tasks.some(t=>t.status==='succeeded'),60);
    await fault('stop_orchestrator');orchestratorStopped=true;await fault('corrupt_object');objectCorrupted=true;await capture('accepted-object-corrupted');
    await fault('release_workers');workersHeld=false;await fault('start_orchestrator');orchestratorStopped=false;
    run=await wait(w=>paused(w),300);assert.equal(run.delivery.length,0);
    const observed=await fault('progress'),work=[...run.tasks,...run.control_jobs];
    assert.ok(observed.integrity_errors?.some(e=>e.exception_type==='ArtifactUnavailable'&&e.error_code==='result_unavailable')&&work.some(t=>t.error_code==='result_unavailable'),'The corrupted encrypted bytes were not actually rejected');
    state.integrity_evidence=observed.integrity_errors;await fault('restore_object');objectCorrupted=false;state.outcome='protocol_passed';
   }
   if(hook==='stale_decision'){
    run=await wait(w=>deciding(w),300);const decision=run.decisions[0];
    const commandURL=BASE+`/api/projects/${state.project}/runs/${run.id}/commands`,body={request_id:uid(),action:'answer',payload:{decision_id:decision.id,snapshot_digest:'0'.repeat(64),action:'approve'}};
    const rejected=await context.request.post(commandURL,{data:body});assert.equal(rejected.status(),202);
    const duplicate=await context.request.post(commandURL,{data:body});assert.equal(duplicate.status(),202);assert.equal((await duplicate.json()).id,(await rejected.json()).id);
    assert.equal((await context.request.post(commandURL,{data:{...body,action:'pause',payload:{}}})).status(),409);
    state.browser_assertions.push('Identical command ID is idempotent; changed content under the same ID is rejected');
    await page.waitForTimeout(2500);assert.equal((await read()).state,'WAITING_USER');await approve('reject');run=await wait(w=>paused(w));state.outcome='protocol_passed';
   }
   if(intervention&&spec.id==='R09'){
    run=await wait(w=>w.tasks.some(t=>t.id==='export'&&t.status==='running'),600);const original=run.id;
    const selected=[];
    for(const identifier of ['prepare_a','prepare_b','clip']){const task=run.tasks.find(t=>t.id===identifier&&t.status==='succeeded');assert.ok(task);selected.push(await reuse(task));}
    const accepted=await appendInput(`改变整体交付范围：复用刚加入的已验收素材${selected.join('、')}，从clip取前${spec.expect.duration}秒并cover居中裁切为${spec.expect.width}×${spec.expect.height} MP4，保留原音频，交付final.mp4及prepare_a按ImageOps.fit生成的128×64 cover.png。请用NEW_RUN_REQUIRED提出完整新目标和授权；旧export正常结束后再到安全点，不要默认取消或发布旧版，不重复亮度处理或剪辑原clip。`,selected);
    run=await wait(w=>w.decisions.some(d=>d.kind==='new_run'),900);assert.equal(run.id,original);assert.equal(run.delivery.length,0);
    const drained=run.tasks.find(t=>t.id==='export');assert.equal(drained.status,'succeeded');
    await approve('approve');assert.notEqual(state.run_id,original);
    state.revision_input=accepted;state.new_scope_boundary={original_run_id:original,successor_run_id:state.run_id,drained_export_revision:drained.revision};persist();
   }else if(intervention&&spec.id==='R17'){
    run=await wait(w=>deciding(w),600);const original=run.id;
    const source=run.tasks.find(task=>task.id==='prepare_a'&&task.status==='succeeded');assert.ok(source);
    const asset=await reuse(source);
    const accepted=await feedback(`将整体交付从64×48改为32×24 PNG。请通过NEW_RUN_REQUIRED提出完整新目标和范围，明确使用新加入的已验收素材${asset}，用 Pillow ImageOps.fit 默认方法生成32×24 PNG，不要重复亮度处理，不再人工Review。确认前不得执行新方向。`,[asset]);
    run=await wait(w=>w.decisions.some(d=>d.kind==='new_run'),900);
    assert.equal(run.id,original);assert.equal(run.delivery.length,0);
    await approve('approve');assert.notEqual(state.run_id,original);
    state.revision_input=accepted;state.new_scope_boundary={original_run_id:original,successor_run_id:state.run_id,asset_id:asset};persist();
   }else if(intervention&&['R04','R05','R06','R07','R08','R11','R15'].includes(spec.id)){
    if(['R11','R15'].includes(spec.id))run=await wait(w=>deciding(w),600);
    else if(['R04','R05','R06'].includes(spec.id))run=await wait(w=>w.tasks.some(task=>task.id==='staging'&&task.status==='running'),600);
    else run=await wait(w=>w.tasks.some(task=>task.id==='export'&&task.status==='running'),600);
    recordSources(run);state.same_run_revision=true;const originalRun=run.id;
    const messages={
     R04:'保留原有128×64 PNG交付，另外从同一张最终图增加final.jpg。不要重做已验收的prepare_a和prepare_b。PNG仍是主交付。当前在途任务正常结束后统一规划，不要取消。',
     R05:'同一目标内，只把prepare_a改为对portrait.png做brightness=1.35。保持prepare_a逻辑ID，使用新revision；prepare_b保持原revision和哈希。更新受影响下游，最终仍按Pillow ImageOps.fit默认方法各64×64并排成128×64 final.png。',
     R06:'同一目标内，把prepare_a的来源替换为已授权rocket.png并做brightness=1.1，程序验收使用新素材的原尺寸。保持prepare_a逻辑ID并创建新revision，prepare_b保持不变，受影响下游重算，最终仍按Pillow ImageOps.fit默认方法各64×64并排成128×64 final.png。',
     R07:'保持视频和全部原验收要求，只把export的cover.png换为已验收prepare_b，以Pillow ImageOps.fit默认方法做128×64封面。修订export的实现和必要输入/依赖，不重新处理clip或两张准备图。仍交付final.mp4与cover.png，主交付是视频。',
     R08:'保持原视频、音频、封面及全部尺寸时长，在export视频左上角(2,2)增加黄色QA字幕，字号20。修订export的实现，保留clip及准备图的原revision。仍交付final.mp4和cover.png，主交付是视频。',
     R11:'请在当前128×64拼图上用Pillow ImageDraw.rectangle画4像素纯红色内边框，包含四个角。保持原目标、素材和技术验收，给export创建新revision；prepare_a、prepare_b不要重算。保留原人工Review，待用户复核新版；仍须通过原技术检查、必要Agent验收及Final Review。',
     R15:'请保持原128×64拼图内容与像素算法不变，给export创建一个新revision重新输出final.png，prepare_a、prepare_b保持原revision。保留原人工Review，待用户复核新版；仍需技术与最终验收。'};
    const input=['R11','R15'].includes(spec.id)?await feedback(messages[spec.id]):await appendInput(messages[spec.id],[],['R05','R06'].includes(spec.id)?['prepare_a']:['export']);
    if(spec.id==='R15'){await page.reload({waitUntil:'domcontentloaded'});await page.locator('#dynamic-current').waitFor({state:'visible'});state.browser_assertions.push('Re-entered the project without creating another task');}
    run=await wait(w=>w.inputs.some(item=>item.id===input.id&&item.status==='APPLIED')||deciding(w),900);
    if(deciding(run))throw Object.assign(Error('Unexpected scope or clarification decision during the already authorized revision'),{model:true});
    assert.equal(run.id,originalRun);state.revision_input=input;state.browser_assertions.push('Feedback used the same Run inbox and drained existing work before replanning');persist();
   }else   if(intervention){
    if(['R10','R12','R16'].includes(spec.id)){
     run=await observeTerminal();
     if(run.state!=='SUCCEEDED'||!run.delivery?.length){const error=new Error('Required prior delivery was not completed before intervention: '+run.state);error.boundary=true;throw error;}
     state.prior_delivery_boundary={run_id:run.id,status:run.state,delivery:run.delivery.map(x=>({file:x.file,sha256:x.sha256}))};persist();
    }
    else if(gated)run=await wait(w=>deciding(w),600);
    else if(['R04','R05','R06'].includes(spec.id))run=await wait(w=>w.tasks.some(t=>t.id==='staging'&&t.status==='running'),600);
    else run=await wait(w=>w.tasks.some(t=>t.id==='export'&&t.status==='running'),600);
    if(spec.id==='R14'){
     const end=Date.now()+180000;let marker;
     while(Date.now()<end){const facts=await fault('progress');if(facts.output_ready_events.length&&facts.output_ready_checkpoints.length){marker=facts;break;}await page.waitForTimeout(300);}
     assert.ok(marker,'Output completion requires both the progress marker and a durable output checkpoint');state.output_completion_boundary={events:marker.output_ready_events,checkpoints:marker.output_ready_checkpoints};
    }
    const sources=run.tasks.filter(t=>t.status==='succeeded'&&t.spec.operation==='image.local_adjust').sort((a,b)=>a.id.localeCompare(b.id));
    assert.ok(sources.length>= (spec.id==='R17'?1:2));
    const selected=[];for(const task of sources.filter(t=>!['R05','R06'].includes(spec.id)||t.id==='prepare_b'))selected.push(await reuse(task));
    if(spec.assets.includes('clip_a.mp4')){const clip=run.tasks.find(t=>t.id==='clip'&&t.status==='succeeded');assert.ok(clip,'Accepted clip required for reuse');selected.push(await reuse(clip));}
    if(gated){await approve('reject');await wait(w=>paused(w));}
    if(spec.id==='R13'){await control('pause');await wait(w=>paused(w));}
    if(!terminal((await read()).state)){await control('cancel');await wait(w=>terminal(w.state));}
    await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(runId=>document.querySelector('#dynamic-current')?.dataset.runId===runId&&!document.querySelector('#dynamic-run-tools')?.hidden,state.run_id);await page.locator('#dynamic-input-form').waitFor({state:'visible'});
    const contextSettings=page.locator('#dynamic-context-settings'),inputAssets=page.locator('#dynamic-input-assets');await contextSettings.waitFor({state:'visible'});if(!await contextSettings.evaluate(node=>node.open))await contextSettings.locator('summary').click();await inputAssets.waitFor({state:'visible'});
    await inputAssets.evaluate((node,values)=>{const chosen=new Set(values);for(const option of node.options)option.selected=chosen.has(option.value);node.dispatchEvent(new Event('change',{bubbles:true}));},selected);
    let revised=`复用已验收图片 prepare_a 在左、prepare_b 在右，不要重复亮度处理。用 Pillow ImageOps.fit 默认方法分别适配到${spec.expect.width/2}×${spec.expect.height}后并排合成${spec.expect.width}×${spec.expect.height}，交付 final.png。`;
    if(spec.id==='R01')revised+='最终文件改名为 renamed.png。';
    if(spec.id==='R02')revised+='最终改为64×32的 final.webp，采用无损WebP。';
    if(spec.id==='R03')revised+='交换顺序，prepare_b 在左、prepare_a 在右。';
    if(spec.id==='R04')revised+='同时交付 final.png 和 final.jpg 两种格式。';
    if(spec.id==='R10')revised+='另外将最终图用 Lanczos 缩小为32×16 thumbnail.png 并交付。';
    if(['R05','R06'].includes(spec.id)){
     const original=await api('GET',`/api/projects/${state.project}`);const name=spec.id==='R05'?'portrait.png':'rocket.png';const asset=original.assets.find(a=>a.name===name),chosen=await page.locator('#dynamic-input-assets').evaluate(node=>[...node.selectedOptions].map(option=>option.value));await selectOptional(page.locator('#dynamic-input-assets'),[...new Set([...chosen,asset.id])]);
     revised=`仅复用已验收 prepare_b 作为右半边；左半边改为用 image.local_adjust 对 ${name} 做 brightness=${spec.id==='R05'?'1.35':'1.1'} 处理，分别用 Pillow ImageOps.fit 默认方法适配为64×64，再合成128×64 final.png。`;
    }
    if(spec.expect.kind==='video')revised=`复用已验收 clip 视频，取前${spec.expect.duration}秒并按 cover 居中裁切为${spec.expect.width}×${spec.expect.height} MP4，保留原音频，交付 final.mp4 和128×64 cover.png。封面用已验收 ${spec.id==='R07'?'prepare_b':'prepare_a'} 图片，以 Pillow ImageOps.fit 默认方法处理。${spec.id==='R08'?'左上角(2,2)放置黄色 QA 字幕，字号20。':''}不要重复剪辑原始 clip_a.mp4 或重新亮度处理图片。`;
    if(spec.id==='R11')revised+='最后用 Pillow ImageDraw.rectangle 在边缘画4像素纯红色边框，边框包括四个角。';
    if(spec.id==='R16')revised+='本轮仅输出 final.jpg；下一轮会再要求其他格式。';
    if(spec.id==='R17')revised='复用已验收的亮度1.1图片，用 Pillow ImageOps.fit 默认方法生成32×24 PNG，程序检查尺寸后交付，不再要求人工验收。';
    if(['R01','R02','R03'].includes(spec.id)){await page.locator('#dynamic-input-content').fill('复用已验收图片，交付128×64 final.png，prepare_a 在左。');await capture('draft-before-change');state.events.push({action:'scope_edited_before_confirmation',at:Date.now()/1000});}
    await start(revised);
    if(spec.id==='R15'){await page.reload({waitUntil:'domcontentloaded'});await page.locator('#dynamic-current').waitFor({state:'visible'});state.browser_assertions.push('Re-entered the project without creating another task');}
    if(spec.id==='R16'){
     run=await observeTerminal();assert.equal(run.state,'SUCCEEDED');await page.locator('#dynamic-input-form').waitFor({state:'visible'});
     await start('复用已验收 prepare_a、prepare_b，分别用 Pillow ImageOps.fit 默认方法适配为64×64并排生成128×64 final.png，再用 Lanczos 生成32×16 thumbnail.png，两张都交付。');
    }
   }
   if(!state.outcome)run=await observeTerminal();
   await unified.after({spec,state,run,fault,persist});
   if(apiStopped){await fault('start_api');apiStopped=false;await page.waitForTimeout(6000);}
   if(run.state==='SUCCEEDED'&&['rolling_plan','parallel_replan'].includes(hook)){
    assert.ok(run.graph_version>=2,'Required rolling planning boundary was not reached');
    const observed=await fault('progress');state.boundary_evidence={graph_version:run.graph_version,attempts:observed.attempts};
    if(hook==='parallel_replan'){
     const attempts=observed.attempts.filter(a=>!a.control_kind&&a.finished);
     assert.ok(attempts.some((a,i)=>attempts.slice(i+1).some(b=>a.started<b.finished&&b.started<a.finished)),'Required parallel execution boundary was not reached');
    }
   }
   if(run.state==='SUCCEEDED'&&hook==='specialist_scope'){
    assert.ok(run.tasks.some(t=>t.type==='agent'&&t.spec.profile==='editing_implementer'&&t.status==='succeeded'),'Required specialist Agent did not run');
    state.browser_assertions.push('Specialist task accepted with frozen profile and declared result');
   }
   if(run.state==='SUCCEEDED'&&hook==='repair_revision'){
    const repaired=run.tasks.find(t=>t.id==='render');assert.ok(repaired,'Repair root task is missing');
    assert.ok(repaired.revision>=1&&repaired.repair_count===1,'Repair did not create exactly one counted root revision');
    const child=run.tasks.find(t=>t.id==='package');assert.ok(child?.revision>=1&&child.status==='succeeded','Dependent task did not move to and complete a new revision');
    state.repair_boundary={task_id:repaired.id,revision:repaired.revision,repair_count:repaired.repair_count,child_revision:child.revision};
    state.browser_assertions.push('Immutable repair revision completed and downstream used the replacement revision');
   }
   if(observerStopped){await fault('start_observer');observerStopped=false;await page.waitForTimeout(6000);}
   if(run.state==='SUCCEEDED'){
    state.artifacts=[];
    for(const item of run.delivery){const response=await context.request.get(BASE+'/'+item.url);assert.equal(response.status(),200);const file=path.join(directory,'output',item.file);fs.writeFileSync(file,await response.body(),{mode:0o600});assert.equal(sha(file),item.sha256);state.artifacts.push({...item,local_file:file});}
    assert.ok(state.artifacts.length,'Succeeded workflow must deliver actual files');await page.waitForFunction(id=>document.querySelector('#dynamic-current')?.dataset.runId===id&&document.querySelector('#dynamic-current')?.dataset.status==='SUCCEEDED',run.id);
    if(hook==='foreign_project'){
     const outsider=await browser.newContext();for(const item of run.delivery)assert.equal((await outsider.request.get(BASE+'/'+item.url)).status(),404);await outsider.close();
     state.browser_assertions.push('An unrelated browser cannot download accepted delivery bytes');
    }
    assert.equal(await page.locator('.dynamic-deliverable').count(),state.artifacts.length);
    state.browser_assertions.push('Displayed delivery count matches persisted delivery');
   const primary=run.delivery.find(a=>a.is_primary)||run.delivery[0];const preview=await context.newPage();
   const inline=/^(image\/(png|jpeg|webp|gif)|video\/|audio\/|text\/(plain|csv)|application\/json)/.test(primary.type);
   if(inline){await preview.goto(BASE+'/'+primary.url,{waitUntil:'domcontentloaded'});state.browser_preview='actual artifact URL';}
   else{
    const downloading=preview.waitForEvent('download');await preview.goto(BASE+'/'+primary.url).catch(e=>{if(!e.message.includes('Download'))throw e;});
    const download=await downloading;const target=path.join(directory,'browser-download.bin');await download.saveAs(target);assert.equal(sha(target),primary.sha256);fs.unlinkSync(target);state.browser_preview='actual browser download, hash verified';
   }
    if(inline&&primary.type.startsWith('image/'))await preview.waitForFunction(()=>document.images.length>0&&[...document.images].every(i=>i.complete&&i.naturalWidth>0));
    if(inline&&/^(video|audio)\//.test(primary.type)){
     await preview.waitForFunction(()=>document.querySelector('video,audio')?.readyState>=1);
     await preview.locator('video,audio').evaluate(async media=>{media.muted=true;await media.play();});
     await preview.waitForFunction(()=>document.querySelector('video,audio')?.currentTime>.2);
     await preview.locator('video,audio').evaluate(media=>media.pause());state.browser_assertions.push('Actual browser media playback advanced beyond 0.2 seconds');
    }
    await preview.screenshot({path:path.join(directory,'artifact-browser.png'),fullPage:true});await preview.close();
    state.outcome=state.outcome||(spec.expect.kind==='protocol'?'protocol_passed':'business_succeeded');
   }else if(!state.outcome){state.outcome='paused_or_failed';state.error_codes=run.tasks.filter(t=>t.error_code).map(t=>t.error_code);}
   await page.waitForFunction(({id,status,version,tasks})=>document.querySelector('#dynamic-current')?.dataset.runId===id&&document.querySelector('#dynamic-current')?.dataset.status===status&&Number(document.querySelector('#dynamic-current')?.dataset.graphVersion)===version&&JSON.stringify([...document.querySelectorAll('#dynamic-tasks > article')].map(t=>t.dataset.status))===JSON.stringify(tasks),{id:run.id,status:run.state,version:run.graph_version,tasks:run.tasks.map(t=>t.status)});
   assert.match(await page.locator('#dynamic-updated').textContent(),/最后更新/);const diagnosticHref=await page.locator('#dynamic-diagnostics').getAttribute('href');assert.ok(diagnosticHref?.includes(run.id)&&diagnosticHref.includes('download=true'));
   const diagnosticResponse=await context.request.get(new URL(diagnosticHref,BASE+'/').href);assert.equal(diagnosticResponse.status(),200);assert.match(diagnosticResponse.headers()['content-disposition']||'',/attachment/);
   state.browser_assertions.push('Progress timestamp and explicit diagnostic download match the current workflow');
   state.browser_assertions.push('Final browser status agrees with authoritative workflow snapshot');
   state.browser_checked=true;await capture('final-browser');persist();
   if(state.artifacts.length&&spec.expect.kind!=='protocol')state.artifact_verification=await probe('verify');
  }
 }catch(error){state.error=error.message;state.outcome=error.boundary?'boundary_not_reached':error.dependency?'dependency_blocked':error.model?'model_or_policy_pause':error.observation?'observation_timeout':'test_failed';
  if(state.run_id){
   try{const latest=await read();await page.waitForFunction(({id,status,version})=>{const node=document.querySelector('#dynamic-current');return node?.dataset.runId===id&&node.dataset.status===status&&Number(node.dataset.graphVersion)>=version;},{id:latest.id,status:latest.state,version:latest.graph_version},{timeout:10000});}
   catch{await page.waitForFunction(()=>document.querySelector('#dynamic-connection')?.textContent.includes('中断'),null,{timeout:6000}).catch(()=>{});}
  }
  try{await capture('failure-browser');state.browser_observation={url:page.url(),error:await page.locator('#dynamic-error').textContent().catch(()=>''),status:await page.locator('#dynamic-status').textContent().catch(()=>'')};state.browser_checked=true;}catch{}persist();}
 finally{
  if(state.unified_boundary?.route_switched){try{await fault('restore_provider_route');state.unified_boundary.route_restored=true;}catch(e){state.restore_error=e.message;}}
  for(const [needed,action,unit] of [[objectCorrupted,'restore_object'],[workersHeld,'release_workers'],[orchestratorStopped,'start_orchestrator'],[observerStopped,'start_observer'],[apiStopped,'start_api'],[!!stoppedWorker,'start_worker',stoppedWorker]])if(needed){try{await fault(action,unit);}catch(e){state.restore_error=e.message;}}
  if(state.project){
   try{state.internal=await probe('final');}catch(e){state.audit_error=e.message;}
   if(spec.required_archive_video_members){
    let inspectionAudit={};try{inspectionAudit=JSON.parse(fs.readFileSync(path.join(directory,'internal-final.json'),'utf8'));}catch{}
    const indexes=(inspectionAudit.inspections||[]).map(row=>{try{return JSON.parse(row.evidence_json);}catch{return {};}})
     .filter(item=>item.coverage==='archive_directory'&&item.contents_verified===false);
    const count=Math.max(0,...indexes.map(item=>new Set((item.members||[]).filter(m=>/\.(mp4|mov|mkv|webm)$/i.test(m.name)).map(m=>m.name)).size));
    state.archive_inspection_boundary={required:spec.required_archive_video_members,observed_video_members:count,passed:count>=spec.required_archive_video_members};
    if(state.archive_inspection_boundary.passed)state.events.push({action:'archive_directory_inspection_verified',video_members:count,at:Date.now()/1000});
    else if(state.outcome==='business_succeeded'){state.outcome='boundary_not_reached';state.error='The required ZIP directory inspection was not observed';}
   }
   if(secondStarted){try{await fault('stop_second_orchestrator');}catch(e){state.restore_error=e.message;}}
   try{
    state.events.push({action:'cleanup_started',at:Date.now()/1000});persist();if(!interrupted)await page.goto('about:blank');
    const listing=await api('GET',`/api/projects/${state.project}/runs`);
    for(const item of listing.runs){if(!terminal(item.state)){await api('POST',`/api/projects/${state.project}/runs/${item.id}/commands`,{request_id:uid(),action:'cancel'});const end=Date.now()+180000;while(Date.now()<end){const current=await api('GET',`/api/projects/${state.project}/runs/${item.id}`);if(terminal(current.state))break;await delay(1000);}}}
    await probe('inspect');state.internal=await probe('final');
    const external=JSON.parse(fs.readFileSync(path.join(directory,'internal-snapshot.json'))).dimensions.external_cleanup.records;
    if(!state.internal?.complete){state.cleanup_retained='Internal evidence is incomplete; retain the project until the audit can be completed';save(path.join(directory,'cleanup.json'),{complete:false,expected_retention:false,reason:state.cleanup_retained});}
    else if(external.some(r=>['unknown','unconfirmed','unsupported'].includes(r.state))){state.cleanup_retained='External state is unresolved; retained durable evidence and project for reconciliation';save(path.join(directory,'cleanup.json'),{complete:false,expected_retention:true,reason:state.cleanup_retained});}
    else{await api('DELETE',`/api/projects/${state.project}`);state.cleanup=await probe('cleanup');}
   }catch(e){state.cleanup_error=e.message;}
  }
  activeStops.delete(cancelForInterrupt);state.finished_at=Date.now()/1000;state.status='finished';persist();await context.close().catch(()=>{});
  // A case failure is evidence, not a reason to abandon full-round coverage.
  // Failed infrastructure restoration can contaminate later cases and must stop dispatch.
  if(state.restore_error)fs.writeFileSync(path.join(ROOT,'pause-new-cases'),'Infrastructure restoration failed for '+spec.id+' '+number+'; restore the isolated environment before continuing\n');
  console.log(JSON.stringify({case:spec.id,outcome:state.outcome,program_passed:state.internal?.program_passed,complete:state.internal?.complete,cleanup:state.cleanup?.complete,attempt:directory}));
 }
 return state;
}

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const serial=cases.filter(c=>globalHooks.has(c.dynamic_hook));
  const ordinary=cases.filter(c=>!serial.includes(c));let index=0;
  const concurrency=Math.max(1,Math.min(6,Number(process.env.DYNAMIC_CASE_JOBS||2)));
  await Promise.all(Array.from({length:concurrency},async()=>{while(index<ordinary.length&&!pauseNewCases()&&!interrupted){const spec=ordinary[index++];await runCase(browser,spec);}}));
  for(const spec of serial){if(pauseNewCases()||interrupted)break;await runCase(browser,spec);}
  if(pauseNewCases())console.log(JSON.stringify({event:'campaign_paused_between_cases',reason:'pause-new-cases marker; unfinished coverage remains not_run'}));
 }finally{await stopPromise;await browser.close().catch(()=>{});if(interrupted)process.exitCode=130;}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
