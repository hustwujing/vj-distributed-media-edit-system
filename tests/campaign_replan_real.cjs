/* R17: browser entry, deployed Jev, real planner/executor; no seeded task state. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const REPO=path.resolve(__dirname,'..'),PY=path.join(REPO,'.venv/bin/python');
const BASE=process.env.VU_CASE_BASE||'http://10.194.49.8/video-use';
const campaign=path.join(REPO,'.cluster/campaigns/20261031-real60');
const parent=path.join(campaign,'runs/R17');fs.mkdirSync(parent,{recursive:true,mode:0o700});
let number=Math.max(0,...fs.readdirSync(parent).filter(n=>/^attempt-\d+$/.test(n)).map(n=>Number(n.split('-')[1])))+1;
let ROOT;
for(;;number++){ROOT=path.join(parent,`attempt-${String(number).padStart(3,'0')}`);try{fs.mkdirSync(ROOT,{mode:0o700});break;}catch(e){if(e.code!=='EEXIST')throw e;}}
const FIXTURE=path.join(campaign,'fixtures/portrait.png');
const save=(name,value)=>fs.writeFileSync(path.join(ROOT,name),JSON.stringify(value,null,2)+'\n',{mode:0o600});
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const runId=new Date().toISOString().replace(/[-:.]/g,'')+'-'+crypto.randomBytes(4).toString('hex');
const state={case:'R17',run_id:runId,status:'running',started_at:Date.now()/1000,entry_mode:'browser_real_replan',planning_source:'real_agent',release:JSON.parse(fs.readFileSync(path.join(REPO,'.cluster/release.json'))),events:[],versions:[],page_errors:[],fixture_input_hashes:{'portrait.png':sha(FIXTURE)},driver_sha256:sha(__filename)};
fs.copyFileSync(__filename,path.join(ROOT,'driver-snapshot.cjs'));
state.helper_sha256={};for(const name of ['campaign_replan_verify.py','campaign_replan_cleanup.py','campaign_interventions.py']){const source=path.join(REPO,'tests',name);state.helper_sha256[name]=sha(source);fs.copyFileSync(source,path.join(ROOT,'snapshot-'+name));}
const spec=JSON.parse(fs.readFileSync(path.join(REPO,'docs/real-case-campaign/cases.json'))).cases.find(c=>c.id==='R17');save('case-definition.json',spec);
const persist=()=>save('state.json',state);persist();
(async()=>{
 let browser,page,context;
 const api=async(method,endpoint,data,expected=200)=>{
  const response=await context.request.fetch(BASE+endpoint,{method,...(data===undefined?{}:{data}),timeout:45000});
  const body=await response.json().catch(()=>({}));
  state.events.push({action:'http',method,path:endpoint,status:response.status(),request_id:response.headers()['x-request-id'],at:Date.now()/1000});
  assert.equal(response.status(),expected,`${method} ${endpoint}: ${JSON.stringify(body)}`);return body;
 };
 const history=()=>api('GET',`/api/projects/${state.project}/agent`);
 const waitTurn=async(id,predicate,seconds=600)=>{
  const end=Date.now()+seconds*1000;let row;
  while(Date.now()<end){row=(await history()).turns.find(t=>t.id===id);if(row&&predicate(row))return row;
   if(row&&['failed','cancelled'].includes(row.status))throw Error(`Unexpected ${id}: ${row.status} ${JSON.stringify(row.diagnostic||row.error)}`);
   await page.waitForTimeout(1000);
  }save('timeout-turn.json',row);throw Error('Timed out waiting for '+id);
 };
 const send=async(message,suffix='/agent/messages',expected=202)=>{
  const response=page.waitForResponse(r=>r.url().endsWith(suffix)&&r.request().method()==='POST',{timeout:45000});
  await page.locator('#agent-input').fill(message);await page.locator('#agent-send').click();
  const r=await response,body=await r.json();assert.equal(r.status(),expected,JSON.stringify(body));return body;
 };
 const confirm=async()=>{
  await page.locator('#agent-execute').waitFor({timeout:30000});
  const response=page.waitForResponse(r=>r.url().endsWith('/agent/messages')&&r.request().method()==='POST');
  await page.locator('#agent-execute').click();const r=await response,body=await r.json();assert.equal(r.status(),202,JSON.stringify(body));return body;
 };
 try{
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});context=await browser.newContext({viewport:{width:1440,height:1100}});page=await context.newPage();
  page.on('pageerror',e=>state.page_errors.push(e.message));
  page.on('response',r=>{if(r.url().includes('/api/')&&r.request().method()==='POST')state.events.push({action:'browser_request',method:'POST',path:new URL(r.url()).pathname,status:r.status(),request_id:r.headers()['x-request-id'],at:Date.now()/1000});});
  const project=await api('POST','/api/projects',{name:'REUSE-QA-'+runId+'-R17'},201);state.project=project.id;
  save('private-session.json',Object.fromEntries((await context.cookies()).map(c=>[c.name,c.value])));persist();
  const uploaded=await context.request.post(BASE+`/api/projects/${state.project}/assets`,{multipart:{file:{name:'portrait.png',mimeType:'image/png',buffer:fs.readFileSync(FIXTURE)}}});assert.equal(uploaded.status(),201,await uploaded.text());
  state.asset=(await uploaded.json()).id;
  await page.goto(BASE+'/chat/'+state.project);await page.locator('#agent-input').waitFor();await page.waitForFunction(()=>document.querySelector('#agent-status')?.classList.contains('online'));
  const plan=await send('请制定一个三步工作流方案，仅做本地处理，等待我确认才执行。第一步用 image.local_adjust 把 portrait.png 亮度调为1.1，程序验收图片完整性即可，保留该中间图供后续复用。第二步生成64×48 PNG预览，必须停下来等我人工验收（review），未经我批准不能开始第三步。第三步原样复制预览为最终PNG并检查尺寸。原图不覆盖。不需要其他审美选择，方案请直接就绪。');
  state.plan_id=plan.id;persist();
  const planDone=await waitTurn(plan.id,t=>t.status==='succeeded');save('plan.json',planDone);
  assert.equal(planDone.plan_readiness.can_execute,true,JSON.stringify(planDone));
  const old=await confirm();state.old_execute=old.id;persist();
  const paused=await waitTurn(old.id,t=>t.status==='paused'&&(t.workflow?.steps||[]).some(s=>s.status==='awaiting_review'));
  save('old-paused.json',paused);
  const prepared=paused.workflow.steps.find(s=>s.status==='succeeded'&&s.operation==='image.local_adjust');assert.ok(prepared,'V1 must have a completed reusable preprocessing step');state.source_step=prepared.id;
  // Real negative examples must preserve the paused source and the typed input.
  for(const message of ['如果重新规划会怎样？','不要重新做，保持当前方案。']){
   await send(message,`/agent/${old.id}/control`,409);
   assert.equal(await page.locator('#agent-input').inputValue(),message);
   const unchanged=(await history()).turns;assert.equal(unchanged.find(t=>t.id===old.id).status,'paused');assert.equal(unchanged.length,2);
  }
  const changed=await send('继续，但把输出改成32×24 PNG，保留原图和已完成的亮度1.1处理，复用该步骤的产物；取消人工预览停点，改为程序验收尺寸后交付。请重新规划。',`/agent/${old.id}/control`,200);
  assert.equal(changed.action,'replan');assert.equal(changed.replanned_from,old.id);assert.equal(changed.old_turn.status,'cancelled');assert.equal(changed.turn.mode,'plan');
  state.new_plan=changed.turn.id;save('replan-response.json',changed);persist();
  const usage=await api('GET',`/api/projects/${state.project}/tasks/${state.new_plan}/usage?scope=run&limit=200`);
  assert.ok(usage.members.some(m=>m.task_id===old.id));assert.ok(usage.members.some(m=>m.task_id===state.new_plan));save('replan-usage.json',usage);
  const newPlan=await waitTurn(state.new_plan,t=>t.status==='succeeded');assert.equal(newPlan.plan_readiness.can_execute,true,JSON.stringify(newPlan));save('new-plan.json',newPlan);
  assert.equal((await history()).turns.filter(t=>t.mode==='execute').length,1,'replan must wait for new confirmation');
  await page.screenshot({path:path.join(ROOT,'new-plan.png'),fullPage:true});
  await page.reload();await page.locator('#agent-execute').waitFor();
  const execute=await confirm();state.execute_id=execute.id;persist();
  const done=await waitTurn(execute.id,t=>t.status==='succeeded');save('execute.json',done);
  const reused=done.workflow?.steps.find(s=>s.reuse_from?.workflow===paused.workflow.id&&s.reuse_from?.step===prepared.id);
  assert.ok(reused&&reused.status==='succeeded','V2 must reuse the successful V1 preprocessing receipt');
  const artifact=done.artifacts.find(a=>a.is_primary&&a.type==='image/png');assert.ok(artifact,'Must publish a primary PNG');
  const download=await context.request.get(new URL(artifact.url,BASE+'/').href);assert.ok(download.ok());const output=path.join(ROOT,'replanned-output.png');fs.writeFileSync(output,await download.body(),{mode:0o600});
  const dimensions=JSON.parse(execFileSync(PY,['-c','from PIL import Image;import json,sys;im=Image.open(sys.argv[1]);im.load();print(json.dumps(list(im.size)))',output],{encoding:'utf8'}));assert.deepEqual(dimensions,[32,24]);
  state.reuse_checks=JSON.parse(execFileSync(PY,[path.join(REPO,'tests/campaign_replan_verify.py'),ROOT],{encoding:'utf8',timeout:45000}));

  const finalUsage=await api('GET',`/api/projects/${state.project}/tasks/${state.execute_id}/usage?scope=run&limit=200`);save('final-usage.json',finalUsage);
  assert.deepEqual(state.page_errors,[]);assert.equal(JSON.parse(fs.readFileSync(path.join(REPO,'.cluster/release.json'))).sha256,state.release.sha256,'Release changed during test');
  state.verification={passed:true,checks:['negative inputs do not mutate','real browser replan','old turn cancelled','parent run retained','fresh confirmation required','V1 output reused','32x24 PNG decoded','no browser errors'],artifact_sha256:sha(output)};
  state.artifacts=[{...artifact,local_file:output,sha256:sha(output)}];state.verification.artifacts=state.artifacts;
  state.content_review={passed:true,method:'actual decoded dimensions and retained successful V1 preprocessing reference'};
  state.status='succeeded';await page.screenshot({path:path.join(ROOT,'final.png'),fullPage:true});
 }catch(error){state.status='failed';state.error=String(error.stack||error);console.error(state.error);process.exitCode=1;
  if(page){await page.screenshot({path:path.join(ROOT,'failure.png'),fullPage:true}).catch(()=>{});try{save('failure-history.json',await history());}catch{}}
 }finally{
  persist();
  try{execFileSync(PY,[path.join(REPO,'tests/campaign_replan_verify.py'),ROOT,'--capture'],{encoding:'utf8',timeout:45000});}catch(error){state.capture_error=String(error.stderr||error.message);}
  try{state.cleanup=JSON.parse(execFileSync(PY,[path.join(REPO,'tests/campaign_replan_cleanup.py'),ROOT,'--base',BASE],{encoding:'utf8',timeout:180000}));}
  catch(error){state.cleanup={passed:false,error:String(error.stderr||error.message)};process.exitCode=1;}
  state.finished_at=Date.now()/1000;state.elapsed_seconds=state.finished_at-state.started_at;persist();save('result.json',state);await browser?.close();
  console.log(JSON.stringify({case:'R17',status:state.status,cleanup:state.cleanup,evidence:ROOT}));
 }
})();
