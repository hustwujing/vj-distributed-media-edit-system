// Real isolated deployment: Manager messages, confirmation, execution and preview.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('playwright');
const repo=path.resolve(__dirname,'..'),out=path.resolve(process.argv[2]||'');
assert.ok(process.argv[2]&&out.startsWith('/data2/wujing03/'));
assert.ok(!fs.existsSync(path.join(out,'state.json')),'Never overwrite a prior live attempt');
const base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend',internal='http://127.0.0.1/distributed-agent-sandbox';
const uid=()=>crypto.randomBytes(16).toString('hex'),terminal=s=>['SUCCEEDED','FAILED','CANCELLED'].includes(s);
const save=(name,data)=>{fs.mkdirSync(out,{recursive:true,mode:0o700});fs.writeFileSync(path.join(out,name),JSON.stringify(data,null,2)+'\n',{mode:0o600});};
const state={total:2,started:0,passed:0,program_failed:0,prompt_failed:0,nonprogram_failed:0,active:0,cases:[],page_errors:[],started_at:Date.now()/1000};
const release=JSON.parse(fs.readFileSync(path.join(repo,'.sandbox-cluster/release.json')));assert.equal(release.namespace,'sandbox-agent');save('release.json',release);
let browser,context,page,currentCase,stop=false;const owned=[];
process.on('SIGINT',()=>stop=true);process.on('SIGTERM',()=>stop=true);
const persist=()=>save('state.json',state);
async function api(method,suffix,data){const cookies=await context.cookies(base+'/');const r=await context.request.fetch(internal+suffix,{method,headers:{Cookie:cookies.map(c=>c.name+'='+c.value).join('; ')},...(data===undefined?{}:{data}),timeout:60000});assert.ok(r.ok(),`HTTP ${r.status()} ${suffix}: ${(await r.text()).slice(0,700)}`);return r.json();}
async function waitRun(project,run,test,label,seconds=180){const deadline=Date.now()+seconds*1000;while(Date.now()<deadline){if(stop)throw Error('Interrupted');const value=await api('GET',`/api/projects/${project}/runs/${run}`);save(currentCase+'-latest.json',value);if(test(value))return value;if(value.state==='FAILED'||value.state==='CANCELLED')throw Error(`${label}: ${value.state} ${value.terminal_notice||value.terminal_reason}`);await page.waitForTimeout(700);}throw Error('Timeout: '+label);}
async function send(content,starting=false){await page.locator('#dynamic-input-content').fill(content);await page.waitForFunction(()=>!document.querySelector('#dynamic-input-submit').disabled);const response=page.waitForResponse(r=>r.request().method()==='POST'&&(starting?/\/runs$/.test(r.url()):/\/inputs$/.test(r.url())));await page.locator('#dynamic-input-submit').click();const r=await response;assert.equal(r.status(),202,await r.text());return r.json();}
async function choose(){await page.locator('#dynamic-input-form').waitFor();await page.locator('#dynamic-execution-settings').evaluate(n=>n.open=true);await page.locator('#dynamic-model-profile').selectOption('claude55');await page.locator('#dynamic-reasoning-effort').selectOption('high');await page.locator('[name=delivery-target]').evaluateAll(nodes=>nodes.forEach(n=>n.checked=false));await page.locator('#dynamic-targets-other').fill('');}
async function settledMessage(project,run,input,expected){return waitRun(project,run,value=>{const item=value.inputs.find(x=>x.id===input.id);if(item?.status==='FAILED')throw Error('Message failed: '+item.reply);if(item&&item.status!=='ROUTING'){assert.ok(expected.includes(item.status),'Unexpected message disposition: '+item.status);return Boolean(item.reply);}return false;},'message '+input.id);}
async function runCase(id,fn){currentCase=id;const record={id,started_at:Date.now()/1000,status:'running'};state.cases.push(record);state.started++;state.active++;persist();console.log(JSON.stringify({event:'started',case:id}));try{await fn(record);assert.deepEqual(state.page_errors,[]);record.status='passed';state.passed++;}catch(e){record.status='needs_analysis';record.error=e.stack||String(e);save(id+'-failure.json',record);throw e;}finally{record.seconds=Date.now()/1000-record.started_at;state.active--;state.average_seconds=state.cases.filter(x=>x.seconds!==undefined).reduce((s,x)=>s+x.seconds,0)/state.cases.filter(x=>x.seconds!==undefined).length;persist();console.log(JSON.stringify({event:'finished',...record}));}}
(async()=>{
 browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});context=await browser.newContext({viewport:{width:1440,height:1000}});page=await context.newPage();page.on('pageerror',e=>{state.page_errors.push(e.message);persist();});
 try{
  await page.goto(base+'/');
  await runCase('MC01-initial-chat',async record=>{
   const project=await api('POST','/api/projects',{name:'Manager只读对话专项 '+uid()});record.project=project.id;
   await page.goto(base+'/chat/'+project.id);await choose();const run=await send('你好，我现在只是和你打个招呼，没有媒体处理任务，请简单回复即可。',true);record.run_id=run.id;owned.push([project.id,run.id]);persist();
   const done=await waitRun(project.id,run.id,r=>r.state==='SUCCEEDED','initial conversation answer');assert.equal(done.terminal_reason,'conversation_answer');assert.equal(done.tasks.length,0);assert.equal(done.delivery.length,0);assert.ok(done.conversation_answer);save('MC01-final.json',done);
   await page.getByRole('heading',{name:'答复',exact:true}).waitFor();await page.reload();await page.getByRole('heading',{name:'答复',exact:true}).waitFor();await page.screenshot({path:path.join(out,'MC01-reentry.png'),fullPage:true});record.checks=['no_business_task','visible_answer','reload_answer'];
  });
  await runCase('MC02-media-messages',async record=>{
   await page.goto(base+'/');const importResponse=page.waitForResponse(r=>r.request().method()==='POST'&&/\/api\/demos\//.test(r.url()));await page.locator('[data-load="local-speed"][data-editor=chat]').first().click();const imported=await(await importResponse).json();
   await page.waitForURL('**/chat/**');const project=new URL(page.url()).pathname.split('/').at(-1);record.project=project;await choose();
   const original=(await page.locator('#dynamic-input-content').inputValue())+'\n最终视频命名为 speed_original.mp4。请先给方案，等我确认后再执行。';
   const run=await send(original,true);record.run_id=run.id;owned.push([project,run.id]);persist();
   await waitRun(project,run.id,r=>r.activity==='PLANNING','initial planning');
   const greeting=await send('谢谢，辛苦了。不用调整任务或方案。');let current=await settledMessage(project,run.id,greeting,['REPLIED']);assert.equal(current.input_barrier_sequence,null);assert.equal(current.input_sequence,0);assert.equal(current.tasks.length,0);save('MC02-planning-chat.json',current);
   current=await waitRun(project,run.id,r=>r.decisions.some(d=>d.kind==='plan_confirmation'),'first concrete plan');const firstPlan=current.decisions.find(d=>d.kind==='plan_confirmation');
   const explain=await send('只解释一下当前方案为什么要分段变速，不修改任何要求，也不开始执行。');current=await settledMessage(project,run.id,explain,['REPLIED']);assert.ok(current.decisions.some(d=>d.id===firstPlan.id));assert.equal(current.input_sequence,0);assert.equal(current.tasks.length,0);save('MC02-confirmation-preserved.json',current);
   await page.reload();await page.locator('.message-reply').filter({hasText:current.inputs.find(i=>i.id===explain.id).reply.slice(0,15)}).waitFor();
   const change=await send('谢谢，另外请把最终视频文件名改为 speed_review.mp4，其他要求保持不变。请先更新方案，等我确认后再执行。');current=await settledMessage(project,run.id,change,['ACCEPTED','PLANNING','APPLIED']);
   current=await waitRun(project,run.id,r=>r.decisions.some(d=>d.kind==='plan_confirmation'&&d.id!==firstPlan.id),'revised concrete plan');assert.ok(!current.decisions.some(d=>d.id===firstPlan.id));assert.equal(current.tasks.length,0);const revised=current.decisions.find(d=>d.kind==='plan_confirmation');assert.match(JSON.stringify(revised.request.delivery_targets),/speed_review\.mp4/);save('MC02-revised-plan.json',current);
   const card=page.locator(`[data-decision="${revised.id}"]`);await card.getByRole('button',{name:'确认方案并执行'}).waitFor();await card.locator('[data-answer=approve]').click();
   current=await waitRun(project,run.id,r=>r.tasks.some(t=>['ready','running','leased','waiting','reviewing'].includes(t.status)),'business execution');const version=current.graph_version;
   const progress=await send('现在进展到哪里了？只汇报实际状态，继续原任务，不修改方案。');current=await settledMessage(project,run.id,progress,['REPLIED']);assert.equal(current.input_barrier_sequence,null);assert.equal(current.graph_version,version);save('MC02-execution-chat.json',current);
   current=await waitRun(project,run.id,r=>r.state==='SUCCEEDED'&&!r.message_pending&&!r.cleanup_pending,'media completion and cleanup',600);assert.ok(current.delivery.some(d=>d.name==='speed_review.mp4'));save('MC02-final.json',current);
   await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();await page.reload();await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();await page.locator('.message-reply').first().waitFor();
   for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});await page.getByRole('button',{name:'预览 speed_review.mp4',exact:true}).first().click();await page.locator('#media-modal').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#media-modal video')?.readyState>=1);await page.locator('#media-modal video').evaluate(v=>v.play());await page.waitForFunction(()=>document.querySelector('#media-modal video').currentTime>0);await page.screenshot({path:path.join(out,`MC02-video-preview-${width}.png`)});await page.getByRole('button',{name:'关闭预览',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#media-modal video').paused);
   }
   record.checks=['planning_chat_no_barrier','confirmation_unchanged_on_explanation','reply_survives_reload','explicit_change_replans','confirmation_before_execution','progress_chat_no_revision','delivery_visible','desktop_mobile_video_playback','cleanup_zero'];
  });
 }finally{
  for(const [project,run] of owned){try{const value=await api('GET',`/api/projects/${project}/runs/${run}`);if(!terminal(value.state))await api('POST',`/api/projects/${project}/runs/${run}/commands`,{request_id:uid(),action:'cancel'});}catch(e){state.cleanup_error=e.message;}}
  await context.storageState({path:path.join(out,'browser-state.json')});fs.chmodSync(path.join(out,'browser-state.json'),0o600);state.finished_at=Date.now()/1000;persist();await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
