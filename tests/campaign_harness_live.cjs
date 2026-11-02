/* Explicit deployed UI Harness checks. Only creates this driver's own project. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('assert/strict');
const BASE=process.env.VU_CASE_BASE||'http://10.194.49.8/video-use';
const out=path.resolve(process.argv[2]);fs.mkdirSync(out,{recursive:true,mode:0o700});
const marker='HARNESS_'+crypto.randomBytes(8).toString('hex');
const report={passed:false,scope:'Deployed browser/API, real SDKs/models, PostgreSQL and encrypted object storage; no workflow state seeding.',turns:[],page_errors:[]};
const save=()=>fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox','--proxy-server=direct://']});
try{
 const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();page.on('pageerror',e=>report.page_errors.push(e.message));
 await page.goto(BASE+'/');const response=await context.request.post(BASE+'/api/projects',{data:{name:'HARNESS-LIVE-'+Date.now()}});assert.equal(response.status(),201);
 report.project=(await response.json()).id;save();await context.storageState({path:path.join(out,'browser-state.json')});fs.chmodSync(path.join(out,'browser-state.json'),0o600);
 await page.goto(BASE+'/chat/'+report.project);const panel=page.locator('#agent-profile-panel');await panel.locator('summary').click();
 const cases=[['claude',`请记住这个标记：${marker}。只回复标记，不需要任何编辑方案或工具。`],
  ['claude','请回复我上一轮要求记住的完整标记，不需要编辑或执行方案。'],
  ['deepseek','请从当前项目公开历史中找出我要求记住的完整标记，只回复标记，不要让我重新提供。'],
  ['deepseek','请再次回复我之前要求记住的完整标记。'],
  ['claude','请从当前项目公开历史中找出我要求记住的完整标记，只回复标记。']];
 let selected=null;
 for(const [profile,prompt] of cases){
  if(selected!==profile){
   await panel.locator('select').selectOption(profile);const changed=page.waitForResponse(r=>r.url().endsWith('/agent/profile')&&r.request().method()==='PUT');
   await panel.getByRole('button',{name:'应用',exact:true}).click();assert.equal((await changed).status(),200);selected=profile;
  }
  await page.locator('#agent-input').fill(prompt);const submitted=page.waitForResponse(r=>r.url().endsWith('/agent/messages')&&r.request().method()==='POST');await page.locator('#agent-send').click();
  const receipt=await submitted;assert.equal(receipt.status(),202);const id=(await receipt.json()).id;let turn;const started=Date.now();
  while(Date.now()-started<600000){
   const r=await context.request.get(BASE+`/api/projects/${report.project}/agent`);assert.equal(r.status(),200);turn=(await r.json()).turns.find(t=>t.id===id);
   if(turn&&['succeeded','failed','cancelled','paused'].includes(turn.status))break;
   await page.waitForTimeout(1000);
  }
  report.turns.push({task:id,profile,status:turn?.status,error:turn?.error,marker_retained:turn?.text?.includes(marker),configuration:turn?.agent_profile,seconds:(Date.now()-started)/1000});save();
  assert.equal(turn?.status,'succeeded');assert.ok(turn.text.includes(marker));assert.equal(turn.agent_profile.name,profile);
  await page.locator(`[data-turn="${id}"] .assistant-text`).filter({hasText:marker}).waitFor();
  assert.equal(await page.locator('#agent-confirmation').isVisible(),false,'Plain memory/chat must not expose execution confirmation');
  await page.screenshot({path:path.join(out,`turn-${report.turns.length}.png`),fullPage:true});
  console.log(JSON.stringify({task:id,profile,status:turn.status,seconds:(Date.now()-started)/1000}));
 }
 assert.deepEqual(report.page_errors,[]);report.passed=true;save();
 console.log(JSON.stringify({passed:true,project:report.project,turns:report.turns.length}));
}catch(error){report.error=String(error.stack||error);save();throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
