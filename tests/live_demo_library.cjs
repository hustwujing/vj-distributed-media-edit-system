/* Actual example-library chat flow. No mocked API/model/operation results. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=process.env.DEMO_CAMPAIGN_REPO||path.resolve(__dirname,'..');
const output=path.resolve(process.argv[2]||'');
assert.ok(process.argv[2]&&output.startsWith('/data2/wujing03/'),'Evidence must be explicitly placed on /data2/wujing03');
const base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend',internal='http://127.0.0.1/distributed-agent-sandbox';
const selected=process.argv[3]&&process.argv[3]!=='all'?new Set(process.argv[3].split(',')):null;
const jobs=8,uid=()=>crypto.randomBytes(16).toString('hex'),sha=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const extraDeliveryTargets=(process.env.DEMO_EXTRA_DELIVERY_TARGETS||'').trim();
const terminal=value=>['SUCCEEDED','FAILED','CANCELLED'].includes(value);
const assertModelConfiguration=value=>{
 assert.equal(value.model,'claude-sonnet-5-5');
 assert.equal(value.roles.media_planner.model,'claude-sonnet-5-5');
 assert.equal(value.roles.visual_quality_reviewer.model,'claude-sonnet-5-5');
 assert.equal(value.roles.editing_implementer.model,'deepseek-v4.1-flash');
 assert.equal(value.roles.editing_implementer.harness,'deepseek_harness');
 assert.equal(value.roles.editing_implementer.reasoning_effort,'low');
};
const save=(file,value)=>{fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});fs.writeFileSync(file,JSON.stringify(value,null,2)+'\n',{mode:0o600});};
const release=JSON.parse(fs.readFileSync(path.join(root,'.sandbox-cluster/release.json')));
assert.equal(release.namespace,'sandbox-agent');assert.equal(release.state,'ready');
assert.ok(release.source.startsWith(root+'/.sandbox-cluster/releases/'));
let stop=false,browser;const active=new Set(),results=[];
process.on('SIGINT',()=>stop=true);process.on('SIGTERM',()=>stop=true);

async function runCase(demo){
 const dir=path.join(output,demo.id);assert.ok(!fs.existsSync(path.join(dir,'state.json')),'Never overwrite an existing attempt');
 const state={case:demo.id,title:demo.title,status:'running',started_at:Date.now()/1000,project:null,run_id:null,run_ids:[],events:[],page_errors:[],http_errors:[],artifacts:[],release_sha:release.sha256};
 const persist=()=>save(path.join(dir,'state.json'),state);persist();save(path.join(dir,'case.json'),demo);
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.setDefaultTimeout(45000);
 const streams=new Map();let lastSignature='',current=null;
 const api=async(method,suffix,data)=>{const cookies=await context.cookies(base+'/');const response=await context.request.fetch(internal+suffix,{method,headers:{Cookie:cookies.map(c=>c.name+'='+c.value).join('; ')},...(data===undefined?{}:{data}),timeout:60000});
  if(!response.ok())throw Error(`HTTP ${response.status()} ${suffix}: ${(await response.text()).slice(0,1000)}`);return response;};
 const read=async()=>{current=await(await api('GET',`/api/projects/${state.project}/runs/${state.run_id}`)).json();state.phase={state:current.state,activity:current.activity,reason:current.terminal_reason,graph_version:current.graph_version};return current;};
 const capture=async name=>{await page.screenshot({path:path.join(dir,name+'.png'),fullPage:true});};
 const cancel=async()=>{if(!state.project)return;for(const run of state.run_ids){try{const value=await(await api('GET',`/api/projects/${state.project}/runs/${run}`)).json();if(!terminal(value.state))await api('POST',`/api/projects/${state.project}/runs/${run}/commands`,{request_id:uid(),action:'cancel'});}catch(error){state.cancel_error=error.message;}}};
 active.add(cancel);
 page.on('pageerror',error=>{state.page_errors.push(error.message);persist();});
 page.on('request',request=>{if(request.url().endsWith('/stream'))streams.set(request.url(),(streams.get(request.url())||0)+1);});
 page.on('response',response=>{if(response.status()>=400)state.http_errors.push({url:new URL(response.url()).pathname,status:response.status()});});
 console.log(JSON.stringify({event:'started',case:demo.id,title:demo.title}));
 try{
  await page.goto(base+'/');await page.locator(`[data-load="${demo.id}"][data-editor=chat]`).first().waitFor();
  await context.storageState({path:path.join(dir,'browser-state.json')});fs.chmodSync(path.join(dir,'browser-state.json'),0o600);
  for(let attempt=0;attempt<8;attempt++){
   const loaded=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith(`/api/demos/${demo.id}/load`));
   await page.locator(`[data-load="${demo.id}"][data-editor=chat]`).first().click();const load=await loaded;
   if(load.status()===201){
    // home.js owns the JSON response and immediately navigates. The browser
    // can discard that body; observe the actual navigation instead of racing it.
    await page.waitForURL(/\/chat\/[a-f0-9]{32}$/);state.project=new URL(page.url()).pathname.split('/').at(-1);break;
   }
   const failure=await load.json();state.events.push({at:Date.now()/1000,action:'import_rejected',...failure});persist();
   assert.ok(load.status()===503&&['API_QUEUE_FULL','API_QUEUE_TIMEOUT'].includes(failure.code)&&failure.outcome==='not_started',JSON.stringify(failure));
   await page.locator('#toast.error').waitFor();assert.ok((await page.locator('#toast').innerText()).includes(failure.detail),'Import rejection was not visible');
   await capture('import-backpressure-'+attempt);await page.waitForTimeout(Math.max(2000,Number(load.headers()['retry-after']||0)*1000)+(attempt*200));
  }
  assert.ok(state.project,'Example import remained unavailable after bounded safe retries');persist();
  await page.waitForURL('**/chat/'+state.project);await page.locator('#dynamic-input-form').waitFor();
  await context.storageState({path:path.join(dir,'browser-state.json')});fs.chmodSync(path.join(dir,'browser-state.json'),0o600);
  const project=await(await api('GET',`/api/projects/${state.project}`)).json();save(path.join(dir,'imported-project.json'),project);
  // Some examples legitimately include additional declared comparison assets.
  for(const source of demo.sources){
   const imported=project.assets.find(asset=>asset.name===source.name);assert.ok(imported,'Imported example source missing: '+source.name);
   for(const track of Object.keys(source.transcripts||{}))assert.ok(project.assets.some(asset=>asset.kind==='json'&&asset.source_asset===imported.id&&asset.audio_track===Number(track)),'Declared cached transcript is not available as an input');
  }
  await page.locator('#dynamic-execution-settings').evaluate(node=>node.open=true);
  await page.locator('#dynamic-model-profile').selectOption('claude55');await page.locator('#dynamic-reasoning-effort').selectOption('high');
  // Model options are populated only after loadProject has restored the draft.
  const importedPrompt=(await page.locator('#dynamic-input-content').inputValue()).trim();
  assert.equal(importedPrompt,project.draft.prompt.trim(),'Restored draft differs from the visible request');
  assert.ok(importedPrompt.includes(demo.prompt.trim()),'Example prompt was lost on import');
  for(const option of demo.options||[])assert.ok(importedPrompt.includes(option.trim()),'Visible example parameters were lost on import');
  await page.locator('[name=delivery-target]').evaluateAll(nodes=>nodes.forEach(node=>node.checked=false));await page.locator('#dynamic-targets-other').fill(extraDeliveryTargets);
  if(extraDeliveryTargets){state.contract_variant={extra_delivery_targets:extraDeliveryTargets};persist();}
  await capture('01-imported');
  const started=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith(`/api/projects/${state.project}/runs`));
  await page.locator('#dynamic-input-submit').click();const start=await started;assert.equal(start.status(),202,await start.text());current=await start.json();state.run_id=current.id;state.run_ids.push(current.id);
  assertModelConfiguration(current.model_configuration);
  state.model_configuration=current.model_configuration;persist();
  const deadline=Date.now()+Number(process.env.DEMO_CASE_TIMEOUT_SECONDS||1800)*1000;
  while(Date.now()<deadline&&!stop){
   await read();const signature=JSON.stringify([current.state,current.graph_version,current.decisions.map(d=>d.id),current.tasks.map(t=>[t.id,t.status,t.error_code])]);
   if(signature!==lastSignature){lastSignature=signature;save(path.join(dir,'snapshot-'+state.events.length+'.json'),current);state.events.push({at:Date.now()/1000,...state.phase});persist();console.log(JSON.stringify({event:'progress',case:demo.id,...state.phase}));}
   if(state.page_errors.length)throw Error('Browser program error: '+state.page_errors[0]);
   if(terminal(current.state))break;
   const plan=current.decisions.find(d=>d.kind==='plan_confirmation');
   if(plan){
    assert.ok(plan.request.delivery_targets.length&&plan.request.steps.length,'Confirmation lacks concrete plan or delivery');
    if(current.graph_version===0)assert.equal(current.tasks.length,0,'Business Tasks existed before confirmation');
    const card=page.locator(`[data-decision="${plan.id}"]`);await card.getByRole('button',{name:'确认方案并执行'}).waitFor();
    assert.match(await card.innerText(),/交付内容/);save(path.join(dir,'plan-'+plan.id+'.json'),plan);await capture('plan-'+plan.id);
    const submitted=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/commands'));
    await card.locator('[data-answer=approve]').click();assert.equal((await submitted).status(),202);
    state.events.push({at:Date.now()/1000,action:'explicit_plan_approval',decision:plan.id});persist();await page.waitForTimeout(1500);continue;
   }
   if(current.state==='WAITING_USER'){
    const decision=current.decisions[0];state.status='awaiting_review';state.waiting_since??=Date.now()/1000;
    const reason=current.terminal_reason,waiting=decision?page.locator(`[data-decision="${decision.id}"]`):page.locator('#dynamic-blocking-action');
    const visibleUntil=Date.now()+45000;let stale=false;
    while(!await waiting.isVisible()&&Date.now()<visibleUntil){
     await page.waitForTimeout(500);await read();
     if(current.state!=='WAITING_USER'||current.terminal_reason!==reason||current.decisions[0]?.id!==decision?.id){stale=true;break;}
    }
    if(stale){state.status='running';delete state.waiting_since;persist();continue;}
    assert.ok(await waiting.isVisible(),'Current pending action stayed invisible for45seconds');
    save(path.join(dir,'attention.json'),{run:current,decision,answer_file:decision?'answer-'+decision.id+'.json':'resolution.json'});persist();
    if(decision){const normalized=text=>text.replace(/\s+/g,' ').trim();assert.ok(normalized(await waiting.innerText()).includes(normalized(decision.request.question||decision.request.reason||'')),'Pending decision text is missing');}
    await capture('needs-attention');
    const answerFile=path.join(dir,decision?'answer-'+decision.id+'.json':'resolution.json');
    if(fs.existsSync(answerFile)){
     const answer=JSON.parse(fs.readFileSync(answerFile));assert.ok(['approve','revise','reject','stop'].includes(answer.action));
     if(answer.action==='stop'){state.status='failed';state.classification=answer.classification||'needs_analysis';state.reason=answer.reason;break;}
     assert.ok(decision,'Resolution requires an actual pending decision');
     const card=page.locator(`[data-decision="${decision.id}"]`);
     if(answer.action==='revise'){
      await card.locator('[data-feedback]').fill(answer.feedback);
      if(answer.attachments?.length)await card.locator('[data-feedback-assets]').selectOption(answer.attachments);
     }
     const sent=page.waitForResponse(response=>response.request().method()==='POST'&&response.url().endsWith('/commands'));
     await card.locator(`[data-answer="${answer.action}"]`).click();const response=await sent;assert.equal(response.status(),202,await response.text());
     const submitted=response.request().postDataJSON().payload;assert.equal(submitted.action,answer.action);assert.equal(submitted.decision_id,decision.id);
     if(answer.action==='revise')assert.equal(submitted.feedback,answer.feedback);
     state.events.push({at:Date.now()/1000,action:'browser_decision_'+answer.action,decision:decision.id});
     fs.renameSync(answerFile,answerFile+'.consumed');state.status='running';delete state.waiting_since;persist();
    }
    await page.waitForTimeout(2500);continue;
   }
   state.status='running';persist();await page.waitForTimeout(2000);
  }
  save(path.join(dir,'final-run.json'),current);
  if(stop)throw Error('Campaign interrupted');
  if(!terminal(current.state)&&state.status!=='failed')throw Error('Required completion timed out');
  if(state.status==='failed')await cancel();
  else if(current.state!=='SUCCEEDED'){state.status='failed';state.classification='needs_analysis';state.reason=current.terminal_reason;await capture('failed');}
  else if(current.terminal_reason==='conversation_answer'){
   const {verifyAnswerView}=require('./demo_library_answer_review.cjs');
   await verifyAnswerView(page,current,capture,()=>streams.get(base+`/api/projects/${state.project}/runs/${state.run_id}/stream`));
   assert.equal(current.cleanup_pending,0,'Answered run resources have not been cleaned');
   save(path.join(dir,'final-run.json'),current);state.browser_verified=true;state.status='responded_pending_review';
  }
  else{
   assert.notEqual(current.terminal_reason,'conversation_answer','A media example was answered without being executed');assert.ok(current.delivery.length,'Completed example has no delivery');
   await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();assert.ok(await page.locator('#dynamic-run-result a[download]').count());
   for(const item of current.delivery){const suffix=new URL(item.url,base+'/').pathname.replace('/distributed-agent-sandbox-backend','');const response=await api('GET',suffix);const bytes=await response.body();assert.equal(sha(bytes),item.sha256);assert.equal(bytes.length,item.size);
    const name=path.basename(item.file);fs.mkdirSync(path.join(dir,'artifacts'),{recursive:true});fs.writeFileSync(path.join(dir,'artifacts',name),bytes);state.artifacts.push({...item,local_file:name});}
   const primary=Math.max(0,current.delivery.findIndex(item=>item.is_primary));
   await page.locator(`#dynamic-run-result [data-result-preview="current-${primary}"]`).click();
   const kind=current.delivery[primary].type||current.delivery[primary].mime||'';
   if(/^(video|audio)\//.test(kind))await page.waitForFunction(()=>{const media=document.querySelector('#dynamic-preview video,#dynamic-preview audio');return media&&media.readyState>=1;});
   else if(kind.startsWith('image/'))await page.waitForFunction(()=>{const image=document.querySelector('#dynamic-preview img');return image&&image.naturalWidth>0;});
   const before=streams.get(base+`/api/projects/${state.project}/runs/${state.run_id}/stream`);await page.waitForTimeout(5000);assert.equal(streams.get(base+`/api/projects/${state.project}/runs/${state.run_id}/stream`),before,'Terminal SSE reconnected');
   await capture('completed');await page.reload();await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();
   await capture('reentered');
   const cleanupUntil=Date.now()+60000;while(current.cleanup_pending&&Date.now()<cleanupUntil){await page.waitForTimeout(1000);await read();}
   assert.equal(current.cleanup_pending,0,'Terminal resources have not been cleaned');
   save(path.join(dir,'final-run.json'),current);state.browser_verified=true;state.status='passed_pending_review';
  }
 }catch(error){state.status='failed';state.classification='needs_analysis';state.reason=error.stack||error.message;await cancel();console.log(JSON.stringify({event:'failure',case:demo.id,reason:error.message}));}
 finally{state.finished_at=Date.now()/1000;state.seconds=state.finished_at-state.started_at;persist();active.delete(cancel);await context.close();results.push(state);console.log(JSON.stringify({event:'finished',case:demo.id,status:state.status,seconds:state.seconds}));}
}

(async()=>{
 fs.mkdirSync(output,{recursive:true,mode:0o700});
 const catalog=await(await fetch(internal+'/api/demos')).json();assert.equal(catalog.length,44);
 save(path.join(output,'catalog.json'),catalog);fs.copyFileSync(__filename,path.join(output,'driver.cjs'));
 const cases=selected?[...selected].map(id=>catalog.find(item=>item.id===id)):catalog;
 if(selected)assert.ok(cases.every(Boolean),'Unknown example requested');
 save(path.join(output,'round.json'),{started_at:Date.now()/1000,total:44,selected:cases.map(d=>d.id),concurrency:jobs,release,catalog_sha256:sha(Buffer.from(JSON.stringify(catalog))),driver_sha256:sha(fs.readFileSync(__filename))});
 browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 try{let cursor=0;const workers=await Promise.allSettled(Array.from({length:Math.min(jobs,cases.length)},async()=>{while(!stop&&cursor<cases.length&&!fs.existsSync(path.join(output,'pause-new-cases'))){const demo=cases[cursor++];await runCase(demo);}}));const errors=workers.filter(item=>item.status==='rejected').map(item=>String(item.reason));if(errors.length){save(path.join(output,'driver-errors.json'),errors);throw Error(errors.join('\n'));}}
 finally{if(stop)await Promise.allSettled([...active].map(cancel=>cancel()));await browser.close();save(path.join(output,'summary.json'),{total:44,started:results.length,results:results.map(s=>({case:s.case,status:s.status,classification:s.classification,seconds:s.seconds})),finished_at:Date.now()/1000});}
})().catch(error=>{console.error(error);process.exitCode=1;});
