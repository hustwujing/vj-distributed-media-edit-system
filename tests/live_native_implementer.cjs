// Actual public UI, frozen configuration, native Task and delivery acceptance.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const repo=path.resolve(__dirname,'..'),out=path.resolve(process.argv[2]||''),caseId=process.argv[3]||'NH01',profile=process.argv[4]||'claude55',engine=process.argv[5]||'native-implementer-v1';
assert.ok(process.argv[2]&&out.startsWith('/data2/wujing03/'));
assert.ok(!fs.existsSync(path.join(out,'state.json')),'Never overwrite live evidence');
const base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend',internal='http://127.0.0.1/distributed-agent-sandbox',uid=()=>crypto.randomBytes(16).toString('hex');
const terminal=v=>['SUCCEEDED','FAILED','CANCELLED'].includes(v.state);
const save=(name,value)=>{fs.mkdirSync(out,{recursive:true,mode:0o700});fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2)+'\n',{mode:0o600});};
const goals={
 NH01:'请由代码编辑师使用Python，为这个视频生成 frame_schedule.csv。只交付这一个UTF-8 CSV，第一行是 frame,time_seconds，后续恰好5行分别为帧序号0到4及按24fps计算的时间（保留6位小数）。不需要重新渲染视频，不调用外部媒体服务。执行引擎和模型按任务设置中的选择。先给方案，等我确认后执行。',
 NH02:'请由代码编辑师编写代码，把这段对白制作成720×1280竖屏、24fps，完整保留人物画面，用原视频放大模糊填满背景；在底部全程添加中文标题“寻找旅伴”，保留原声音和完整9秒时长。只交付portrait.mp4。执行引擎和模型按任务设置中的选择。先给方案，等我确认后执行。',
};
assert.ok(goals[caseId],'Unknown case');
let stop=false;process.on('SIGINT',()=>stop=true);process.on('SIGTERM',()=>stop=true);
const state={case:caseId,profile,expected_engine:engine,status:'running',started_at:Date.now()/1000,events:[],page_errors:[]};
save('state.json',state);save('release.json',JSON.parse(fs.readFileSync(path.join(repo,'.sandbox-cluster/release.json'))));
save('driver-binding.json',{sha256:crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),goal:goals[caseId]});fs.copyFileSync(__filename,path.join(out,'driver.cjs'));
(async()=>{
 const browser=await chromium.launch({headless:true,handleSIGINT:false,handleSIGTERM:false,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 const api=async(method,suffix,data)=>{const cookies=await context.cookies(base+'/');const response=await context.request.fetch(internal+suffix,{method,headers:{Cookie:cookies.map(c=>c.name+'='+c.value).join('; ')},...(data===undefined?{}:{data}),timeout:60000});assert.ok(response.ok(),`${response.status()} ${suffix}: ${(await response.text()).slice(0,800)}`);return response;};
 const read=async()=>{const value=await(await api('GET',`/api/projects/${state.project}/runs/${state.run_id}`)).json();save('latest.json',value);return value;};
 page.on('pageerror',e=>{state.page_errors.push(e.message);save('state.json',state);});
 try{
  await page.goto(base+'/');const loaded=page.waitForResponse(r=>r.request().method()==='POST'&&/\/api\/demos\/agent-portrait\/load$/.test(r.url()));await page.locator('[data-load="agent-portrait"][data-editor=chat]').first().click();assert.equal((await loaded).status(),201);await page.waitForURL('**/chat/**');state.project=new URL(page.url()).pathname.split('/').at(-1);
  await page.locator('#dynamic-input-form').waitFor();await page.locator('#dynamic-execution-settings').evaluate(n=>n.open=true);
  const choices=await(await api('GET',`/api/projects/${state.project}/runs/capabilities`)).json();save('capabilities.json',choices);
  await page.locator('#dynamic-model-profile').selectOption(profile);await page.locator('#dynamic-reasoning-effort').selectOption('high');
  await page.locator('[name=delivery-target]').evaluateAll(ns=>ns.forEach(n=>n.checked=false));await page.locator('#dynamic-targets-other').fill('');await page.locator('#dynamic-input-content').fill(goals[caseId]);
  const started=page.waitForResponse(r=>r.request().method()==='POST'&&/\/runs$/.test(r.url()));await page.locator('#dynamic-input-submit').click();const response=await started;assert.equal(response.status(),202,await response.text());let current=await response.json();state.run_id=current.id;state.model_configuration=current.model_configuration;
  await context.storageState({path:path.join(out,'browser-state.json')});fs.chmodSync(path.join(out,'browser-state.json'),0o600);save('state.json',state);
  console.log(JSON.stringify({event:'started',case:caseId,profile,project:state.project,run:state.run_id}));
  const deadline=Date.now()+900000;let signature='',reentered=false;
  while(Date.now()<deadline&&!stop){
   current=await read();const now=JSON.stringify([current.state,current.activity,current.graph_version,current.tasks.map(t=>[t.id,t.status,t.error_code])]);
   if(now!==signature){signature=now;state.events.push({at:Date.now()/1000,state:current.state,activity:current.activity});save('snapshot-'+state.events.length+'.json',current);save('state.json',state);console.log(JSON.stringify(state.events.at(-1)));}
   assert.deepEqual(state.page_errors,[]);
   const plan=current.decisions.find(d=>d.kind==='plan_confirmation');
   if(plan){assert.ok(plan.request.steps.length&&plan.request.delivery_targets.length);if(current.graph_version===0)assert.equal(current.tasks.length,0);const card=page.locator(`[data-decision="${plan.id}"]`);await card.locator('[data-answer=approve]').waitFor();await page.screenshot({path:path.join(out,'plan-'+plan.id+'.png'),fullPage:true});await card.locator('[data-answer=approve]').click();await page.waitForTimeout(500);continue;}
   if(!reentered&&current.tasks.some(t=>t.type==='agent'&&t.status==='running')){await page.reload();await page.locator('#dynamic-input-form').waitFor();reentered=true;state.reentered_during_execution=true;}
   if(terminal(current))break;
   if(current.state==='WAITING_USER'){await page.screenshot({path:path.join(out,'attention.png'),fullPage:true});throw Error('Unexpected intervention: '+JSON.stringify(current.blocking_action||current.decisions));}
   await page.waitForTimeout(700);
  }
  if(stop)throw Error('User interrupted');assert.equal(current.state,'SUCCEEDED',JSON.stringify(current.terminal_notice||current.terminal_reason));
  const tasks=current.tasks.filter(t=>t.type==='agent'&&t.spec.profile==='editing_implementer');assert.ok(tasks.length,'Case did not execute a code editor');
  const usage=await(await api('GET',`/api/projects/${state.project}/runs/${state.run_id}/usage`)).json();save('usage.json',usage);
  const actual=usage.harness_runs.filter(row=>row.profile_name==='editing_implementer');assert.ok(actual.length,'Missing actual specialist execution receipt');
  const expected=engine==='native-implementer-v1'?current.model_configuration.harness:engine;
  assert.ok(actual.every(row=>row.harness===expected),JSON.stringify(actual));
  for(const task of tasks){if(engine==='native-implementer-v1')assert.equal(task.execution_location,'opensandbox');}
  await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();await page.reload();await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();
  for(const item of current.delivery){const suffix=new URL(item.url,base+'/').pathname.replace('/distributed-agent-sandbox-backend','');const bytes=await(await api('GET',suffix)).body();assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),item.sha256);fs.mkdirSync(path.join(out,'artifacts'),{recursive:true});fs.writeFileSync(path.join(out,'artifacts',path.basename(item.name||item.file)),bytes);}
  if(caseId==='NH01'){assert.equal(current.delivery.length,1);const csv=fs.readFileSync(path.join(out,'artifacts','frame_schedule.csv'),'utf8').replace(/^\uFEFF/,'').trim().split(/\r?\n/);assert.equal(csv[0],'frame,time_seconds');assert.equal(csv.length,6);for(let i=0;i<5;i++)assert.equal(csv[i+1],`${i},${(i/24).toFixed(6)}`);state.independent_content_verified=true;}
  if(caseId==='NH02'){await page.getByRole('button',{name:'预览 portrait.mp4',exact:true}).first().click();await page.locator('#media-modal').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#media-modal video')?.readyState>=1);await page.locator('#media-modal video').evaluate(v=>v.play());await page.waitForFunction(()=>document.querySelector('#media-modal video').currentTime>0);await page.getByRole('button',{name:'关闭预览',exact:true}).click();state.playback_verified=true;}
  await page.screenshot({path:path.join(out,'completed.png'),fullPage:true});state.status=caseId==='NH01'?'passed':'passed_pending_review';save('final-run.json',current);
 }catch(error){state.status='needs_analysis';state.error=error.stack||String(error);console.error(error);process.exitCode=1;}
 finally{
  if(state.run_id){try{let current=await read();if(!terminal(current))await api('POST',`/api/projects/${state.project}/runs/${state.run_id}/commands`,{action:'cancel',request_id:uid()});const until=Date.now()+60000;do{current=await read();if(terminal(current)&&!current.cleanup_pending)break;await page.waitForTimeout(700);}while(Date.now()<until);state.cleanup_ok=terminal(current)&&!current.cleanup_pending;save('cleanup.json',{state:current.state,pending:current.cleanup_pending});}catch(e){state.cleanup_error=e.message;}}
  state.seconds=Date.now()/1000-state.started_at;state.finished_at=Date.now()/1000;save('state.json',state);console.log(JSON.stringify({event:'finished',case:caseId,status:state.status,seconds:state.seconds}));await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
