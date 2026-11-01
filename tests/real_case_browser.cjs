/* Explicit real user UI campaign. Never imported by unit test discovery. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs');const path=require('node:path');const crypto=require('node:crypto');
const DRIVER_SHA=crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex');const {execFileSync}=require('node:child_process');
const REPO=path.resolve(__dirname,'..');const ROOT=path.join(REPO,'.cluster/campaigns/20261031-real60');
const BASE=process.env.VU_CASE_BASE||'http://10.194.49.8/video-use';
const PY=path.join(REPO,'.venv/bin/python');const manifest=JSON.parse(fs.readFileSync(path.join(REPO,'docs/real-case-campaign/cases.json')));
const wanted=(process.argv[2]||'S01,S02').split(',');const concurrency=Number(process.env.VU_CASE_CONCURRENCY||2);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function save(file,value){fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file+'.new',JSON.stringify(value,null,2)+'\n',{mode:0o600});fs.renameSync(file+'.new',file);}
function append(file,value){fs.appendFileSync(file,JSON.stringify(value)+'\n',{mode:0o600});}
function release(){return JSON.parse(fs.readFileSync(path.join(REPO,'.cluster/release.json')));}
function probe(action,id,out){try{const args=[path.join(REPO,'tests/campaign_probe.py'),action,id];if(out)args.push(out);return JSON.parse(execFileSync(PY,args,{cwd:REPO,encoding:'utf8',timeout:action==='verify'?180000:action==='interrupt_worker'?35000:15000,maxBuffer:8*1024*1024}));}catch(error){return {error:String(error.message).slice(0,400)};}}
async function lockedCase(browser,spec){
 const folder=path.join(ROOT,'observer-locks');fs.mkdirSync(folder,{recursive:true});const file=path.join(folder,spec.id+'.json');const token=crypto.randomUUID();
 for(let attempt=0;attempt<3;attempt++){
  try{const fd=fs.openSync(file,'wx',0o600);fs.writeFileSync(fd,JSON.stringify({pid:process.pid,token}));fs.closeSync(fd);break;}
  catch(error){if(error.code!=='EEXIST')throw error;let owner;try{owner=JSON.parse(fs.readFileSync(file));}catch{throw Error('Invalid observer lock; inspect '+file);}
   try{process.kill(owner.pid,0);throw Error('Case '+spec.id+' already has a live observer PID '+owner.pid);}
   catch(check){if(check.code!=='ESRCH')throw check;fs.unlinkSync(file);}
   if(attempt===2)throw Error('Could not acquire observer lock');
  }
 }
 try{return await runCase(browser,spec);}finally{try{if(JSON.parse(fs.readFileSync(file)).token===token)fs.unlinkSync(file);}catch{}}
}
async function runCase(browser,spec){
 const caseRoot=path.join(ROOT,'runs',spec.id);fs.mkdirSync(caseRoot,{recursive:true});
 const previous=fs.readdirSync(caseRoot).filter(n=>/^attempt-\d+$/.test(n)).sort();
 const prior=previous.length?path.join(caseRoot,previous.at(-1),'state.json'):null;
 let state=prior&&fs.existsSync(prior)?JSON.parse(fs.readFileSync(prior)):null;
 if(state&&state.status==='succeeded'&&!process.env.VU_CASE_RERUN){console.log(JSON.stringify({case:spec.id,event:'already_complete'}));return;}
 const resume=state&&['running','interrupted'].includes(state.status)&&!process.env.VU_CASE_RERUN;
 if(process.env.VU_CASE_RESUME_ONLY==='1'&&!resume)throw Error('Resume-only requires an existing live/interrupted attempt');
 const runDir=resume?path.dirname(prior):path.join(caseRoot,'attempt-'+String(previous.length+1).padStart(3,'0'));
 fs.mkdirSync(runDir,{recursive:true});fs.mkdirSync(path.join(runDir,'output'),{recursive:true});
 state=resume?state:{case:spec.id,title:spec.title,status:'running',started_at:new Date().toISOString(),release:release(),driver_sha256:DRIVER_SHA,prompt:spec.prompt,events:[],page_errors:[],http_errors:[],network_errors:[],console_errors:[]};
 const stateFile=path.join(runDir,'state.json');const storage=path.join(runDir,'browser-state.json');
 if(resume){state.events.push({action:'observer_resumed',previous_status:state.status,previous_error:state.error||null,at:new Date().toISOString()});state.status='running';delete state.error;}
 const context=await browser.newContext({viewport:{width:1440,height:1000},...(resume&&fs.existsSync(storage)?{storageState:storage}:{})});
 const page=await context.newPage();page.setDefaultTimeout(30000);
 state.network_errors=state.network_errors||[];state.console_errors=state.console_errors||[];
 page.on('console',message=>{if(message.type()==='error')state.console_errors.push(message.text().slice(0,1000));});
 page.on('requestfailed',request=>{state.network_errors.push({path:new URL(request.url()).pathname,error:request.failure()?.errorText});});
 page.on('pageerror',error=>{state.page_errors.push(error.message);save(stateFile,state);});
 page.on('response',response=>{try{const url=new URL(response.url());if(url.origin===new URL(BASE).origin&&url.pathname.includes('/api/')){const entry={at:new Date().toISOString(),method:response.request().method(),path:url.pathname,status:response.status(),request_id:response.headers()['x-request-id']||null};append(path.join(runDir,'http.jsonl'),entry);if(entry.status>=400)state.http_errors.push(entry);}}catch{}});
 const start=Date.now();let stoppedWorker=null,lastProbe=0;
 if(resume)await persist();
 async function persist(){save(stateFile,state);await context.storageState({path:storage});fs.chmodSync(storage,0o600);}
 async function getTurns(){const r=await page.request.get(BASE+`/api/projects/${state.project}/agent`,{timeout:30000});if(!r.ok())throw Error('Agent state HTTP '+r.status());return (await r.json()).turns;}
 async function submit(mode,override=null){
  const response=page.waitForResponse(r=>r.url().endsWith(`/api/projects/${state.project}/agent/messages`)&&r.request().method()==='POST',{timeout:60000});
  if(mode==='plan'){await page.locator('#agent-input').fill(override||spec.prompt);await page.locator('#agent-send').click();}
  else{await page.locator('#agent-confirmation').waitFor({state:'visible',timeout:30000});await page.locator('#agent-execute').click();}
  const r=await response;const value=await r.json();if(!r.ok())throw Error('Submission rejected: '+JSON.stringify(value));
  state[mode+'_id']=value.id;state[mode+'_release']=release();state.events.push({action:mode,message:mode==='plan'?(override||spec.prompt):'确认上述方案，请执行并检查生成结果。',task:value.id,request_id:r.headers()['x-request-id'],at:new Date().toISOString()});await persist();
  console.log(JSON.stringify({case:spec.id,event:mode+'_submitted',task:value.id}));return value;
 }
 async function control(text){const requested=new Date().toISOString();const response=page.waitForResponse(r=>r.url().endsWith('/control')&&r.request().method()==='POST',{timeout:60000});await page.locator('#agent-input').fill(text);await page.locator('#agent-send').click();const reply=await response;const body=await reply.json();if(!reply.ok())throw Error('Control rejected: '+JSON.stringify(body));state.events.push({action:text,requested_at:requested,at:new Date().toISOString(),request_id:reply.headers()['x-request-id'],response_status:reply.status(),task_status:body.turn?.status||body.status});await persist();}
 async function terminal(id,mode){
  const observationStart=Date.now();
  let signature='';let requestedPause=false,pauseStarted=0;
  while(Date.now()-observationStart<manifest.rules.case_timeout_seconds*1000){
   const turns=await getTurns();const turn=turns.find(t=>t.id===id);if(!turn)throw Error('Task disappeared');
   const current=JSON.stringify({status:turn.status,steps:turn.workflow?.steps?.map(s=>[s.id,s.status,s.revision,s.attempts])});
   if(current!==signature){signature=current;const record={at:new Date().toISOString(),task:id,mode,status:turn.status,workflow:turn.workflow?.status,steps:turn.workflow?.steps?.map(s=>({id:s.id,status:s.status,revision:s.revision,attempts:s.attempts})),visible_progress:await page.locator('#live-status').innerText().catch(()=>''),scheduling:turn.execution?.scheduling};append(path.join(runDir,'progress.jsonl'),record);save(path.join(runDir,mode+'-latest.json'),turn);console.log(JSON.stringify({case:spec.id,event:'progress',mode,status:turn.status,workflow:turn.workflow?.status}));}
   if(Date.now()-lastProbe>30000){lastProbe=Date.now();probe('task',id,path.join(runDir,mode+'-diagnosis.json'));}
   if(mode==='execute'&&spec.hook==='pause_resume'&&!state.pause_completed&&turn.workflow){
    if(!requestedPause&&turn.workflow.steps.some(s=>s.status==='succeeded')&&turn.workflow.steps.some(s=>!['succeeded','cancelled'].includes(s.status))){await control('暂停任务');requestedPause=true;pauseStarted=Date.now();}
    if(requestedPause&&turn.status==='paused'){state.pause_observed=true;await page.screenshot({path:path.join(runDir,'paused.png'),fullPage:true});await control('继续任务');state.pause_completed=true;await persist();continue;}
    if(requestedPause&&!state.pause_completed&&Date.now()-pauseStarted>180000)throw Error('Pause did not reach a safe boundary');
   }
   if(mode==='execute'&&spec.hook==='worker_restart'&&!state.restart_injected&&turn.workflow){
    const result=probe('restart_candidate',id);
    if(result.ready){const injected=probe('interrupt_worker',id);const proof=injected.attempted?injected:probe('fault_status',id);state.fault_injection=proof;if(proof.attempted&&proof.unit)stoppedWorker=proof.unit;await persist();if(!proof.stopped)throw Error('Fault injection was not confirmed; stopped to prevent repeated faults');state.restart_injected=proof;await persist();console.log(JSON.stringify({case:spec.id,event:'worker_interrupted',unit:stoppedWorker}));}
   }
   if(['succeeded','failed','cancelled'].includes(turn.status))return turn;
   if(turn.status==='paused'&&!(mode==='execute'&&spec.hook==='pause_resume'&&requestedPause&&!state.pause_completed)){
    // A real quality/user decision must be reviewed; never auto-approve it.
    state.paused_turn=turn;throw Error('Task paused: '+(turn.error||turn.workflow?.error||'requires review'));
   }
   await delay(spec.hook?1000:2500);
  }
  state.status='interrupted';throw Error('Case observation timeout; task retained, not resubmitted');
 }
 try{
  if(!state.project){await page.goto(BASE+'/',{waitUntil:'domcontentloaded'});const r=await page.request.post(BASE+'/api/projects',{data:{name:`REAL60-${spec.id}-${spec.title}`}});if(!r.ok())throw Error(await r.text());state.project=(await r.json()).id;await persist();}
  await page.goto(BASE+'/chat/'+state.project,{waitUntil:'domcontentloaded'});await page.locator('#agent-input').waitFor();
  if(!state.uploaded){
   if(spec.assets.length){await page.locator('#asset-library input[type=file]').setInputFiles(spec.assets.map(n=>path.join(ROOT,'fixtures',n)));await page.waitForFunction(expected=>!document.querySelector('#asset-library input[type=file]').disabled&&(document.querySelectorAll('#asset-library .asset-entry').length===expected||Boolean(document.querySelector('#toast.error:not([hidden])'))),spec.assets.length,{timeout:180000});if(await page.locator('#asset-library .asset-entry').count()!==spec.assets.length)throw Error('Upload failed: '+await page.locator('#toast').innerText());}
   state.uploaded=true;await persist();await page.screenshot({path:path.join(runDir,'input.png'),fullPage:true});
  }
  if(!state.plan_id)await submit('plan',state.submission_prompt||null);
  let plan=await terminal(state.plan_id,'plan');
  while(true){
   save(path.join(runDir,'plan.json'),plan);save(path.join(runDir,'plan-history',plan.id+'.json'),plan);
   if(plan.status!=='succeeded')throw Error('Planning failed: '+plan.error);
   if(!state.execute_id&&process.env.VU_CASE_AUTO_APPROVE==='1'){
    const missing=(spec.allowed_external_operations||[]).filter(op=>!plan.execution_intent?.operations?.includes(op));
    if(missing.length){
     if(state.intent_correction_sent)throw Error('Revised plan still omits required service operations: '+missing.join(','));
     state.intent_correction_sent=true;await persist();
     await submit('plan','请修正执行意图：本需求明确要求实际调用 '+missing.join(',')+'，不能把受控服务调用声明为script。请保持原需求和交付标准，声明正确operation；单次服务加本地格式整理可用single，多阶段使用workflow。请重新给方案供确认。');
     plan=await terminal(state.plan_id,'plan');continue;
    }
   }
   if(state.execute_id||process.env.VU_CASE_AUTO_APPROVE==='1')break;
   state.awaiting_plan_review=true;await persist();console.log(JSON.stringify({case:spec.id,event:'awaiting_plan_review',plan:plan.id,run:runDir}));
   let decision=null;const approval=path.join(runDir,'approval.json');const reviewStart=Date.now();
   while(Date.now()-reviewStart<manifest.rules.case_timeout_seconds*1000){
    if(fs.existsSync(approval)){const value=JSON.parse(fs.readFileSync(approval));if(value.plan_id===plan.id){decision=value;break;}}
    await delay(1000);
   }
   if(!decision){state.status='interrupted';throw Error('Plan review timeout; execution was not submitted');}
   state.awaiting_plan_review=false;state.events.push({action:'plan_review',decision,at:new Date().toISOString()});await persist();
   if(decision.decision==='approve')break;
   if(decision.decision!=='revise'||!decision.message)throw Error('Plan review declined');
   await submit('plan',decision.message);plan=await terminal(state.plan_id,'plan');
  }
  const external=new Set(['audio.transcribe','image.generate_from_text','video.generate_from_text','video.generate_from_image','image.decompose','image.edit','image.erase','image.edit_region']);
  const unapproved=(plan.execution_intent?.operations||[]).filter(op=>external.has(op)&&!(spec.allowed_external_operations||[]).includes(op));
  if(unapproved.length)throw Error('Plan proposes unapproved external operations: '+unapproved.join(','));
  if(!plan.execution_intent)throw Error('Plan did not declare an execution intent');
  if(spec.workflow_min_steps&&plan.execution_intent.route!=='workflow')throw Error('Complex case requires declared persistent workflow');
  for(const operation of external){const proposed=(plan.execution_intent.operations||[]).filter(x=>x===operation).length,allowed=(spec.allowed_external_operations||[]).filter(x=>x===operation).length;if(proposed>allowed)throw Error('Plan exceeds authorized external-call count for '+operation);}
  if(process.env.VU_CASE_AUTO_APPROVE==='1'&&!state.execute_id){state.events.push({action:'plan_review',decision:{plan_id:plan.id,decision:'approve',method:'automated_contract_check',at:Date.now()/1000},at:new Date().toISOString()});await persist();}

  if(!state.execute_id)await submit('execute');
  const turn=await terminal(state.execute_id,'execute');save(path.join(runDir,'execute.json'),turn);
  if(turn.status!=='succeeded')throw Error('Execution failed: '+turn.error);
  if(spec.workflow_min_steps&&(!turn.workflow||turn.workflow.steps.length<spec.workflow_min_steps))throw Error('Expected at least '+spec.workflow_min_steps+' persistent steps');
  if(spec.hook==='pause_resume'&&!state.pause_completed)throw Error('Pause/resume was not exercised');
  if(spec.hook==='worker_restart'&&!state.restart_injected)throw Error('Node interruption was not exercised');
  state.artifacts=[];
  for(const [i,a] of (turn.artifacts||[]).entries()){
   if(!a.url)continue;const url=new URL(a.url,BASE+'/');if(url.origin!==new URL(BASE).origin)throw Error('Unexpected artifact origin');
   const response=await page.request.get(url.href,{timeout:120000});if(!response.ok())throw Error('Artifact download HTTP '+response.status());
   const name=String(i).padStart(3,'0')+'-'+path.basename(a.name||url.pathname);const target=path.join(runDir,'output',name);fs.writeFileSync(target,await response.body(),{mode:0o600});state.artifacts.push({...a,local_file:target});
  }
  if(!state.artifacts.length)throw Error('No downloadable output');
  await delay(1000);state.primary_title=await page.locator('#latest-delivery').getAttribute('title');
  if(await page.locator('#latest-delivery').isVisible()){
   await page.locator('#latest-delivery').click();
   await page.waitForFunction(()=>{const d=document.querySelector('dialog[open]');if(!d)return true;const img=d.querySelector('img');const media=d.querySelector('video,audio');return img?img.complete&&img.naturalWidth>0:media?media.readyState>=1||Boolean(media.error):true;},{},{timeout:30000});
   const error=await page.evaluate(()=>document.querySelector('dialog[open] video,dialog[open] audio')?.error?.message||null);if(error)throw Error('Browser media preview failed: '+error);
  }
  await page.screenshot({path:path.join(runDir,'result.png'),fullPage:true});
  if(state.page_errors.length)throw Error('Browser page errors: '+state.page_errors.join('; '));
  save(stateFile,state);
  const verification=probe('verify',runDir);save(path.join(runDir,'verification.json'),verification);state.verification=verification;
  if(!verification.passed)throw Error('Artifact verification failed: '+JSON.stringify(verification.errors||verification));
  state.status='succeeded';state.content_review='pending';state.finished_at=new Date().toISOString();
 }catch(error){state.error=String(error.stack||error.message);if(state.status!=='interrupted')state.status='failed';state.finished_at=new Date().toISOString();await page.screenshot({path:path.join(runDir,'failure.png'),fullPage:true}).catch(()=>{});}
 finally{
  if(stoppedWorker||state.restart_injected?.unit||state.fault_injection?.unit)state.restore=probe('restore_worker',stoppedWorker||state.restart_injected?.unit||state.fault_injection.unit);
  for(const key of ['plan_id','execute_id'])if(state[key])probe('task',state[key],path.join(runDir,key+'-final-diagnosis.json'));
  state.trace_archive=probe('archive',runDir);
  state.elapsed_seconds=Math.round((Date.now()-Date.parse(state.started_at))/1000);await persist().catch(()=>save(stateFile,state));await context.close();
  console.log(JSON.stringify({case:spec.id,event:'case_finished',status:state.status,seconds:state.elapsed_seconds,error:state.error?.split('\n')[0]}));
 }
}
(async()=>{
 const selected=wanted.map(id=>{const s=manifest.cases.find(c=>c.id===id);if(!s)throw Error('Unknown case '+id);return s;});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--proxy-server=direct://','--proxy-bypass-list=*']});
 try{let next=0;await Promise.all(Array.from({length:Math.min(concurrency,selected.length)},async()=>{while(next<selected.length){if(fs.existsSync(path.join(ROOT,'PAUSE'))&&process.env.VU_CASE_RESUME_ONLY!=='1')return;const spec=selected[next++];await lockedCase(browser,spec);}}));}
 finally{await browser.close();}
})().catch(error=>{console.error(String(error.stack||error));process.exitCode=1;});
