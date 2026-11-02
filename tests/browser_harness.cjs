const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('fs'),assert=require('assert/strict');
const config=JSON.parse(fs.readFileSync(process.argv[2]));
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.addCookies(config.cookies);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(config.base+'/chat/'+config.project);
  const panel=page.locator('#agent-profile-panel');await panel.locator('summary').click();
  const select=panel.locator('select'),apply=panel.getByRole('button',{name:'应用',exact:true});
  await select.selectOption('claude');await apply.waitFor({state:'visible'});
  const draft='Unsent new requirement remains while switching';await page.locator('#agent-input').fill(draft);
  const response=page.waitForResponse(r=>r.url().endsWith('/harness-switch')&&r.request().method()==='POST');
  await apply.click();assert.equal((await response).status(),202);
  await panel.getByText(/已请求切换/).waitFor();
  assert.equal(await page.locator('#agent-input').inputValue(),draft);
  await page.screenshot({path:config.output+'/switch-pending.png',fullPage:true});
  await panel.getByRole('button',{name:'撤销切换'}).click();await panel.getByText(/切换已取消/).waitFor();
  await select.selectOption('claude');await apply.click();await panel.getByText(/已请求切换/).waitFor();
  fs.writeFileSync(config.output+'/reconcile','1');await panel.getByText(/后续 Agent 执行已切换/).waitFor({timeout:15000});
  assert.equal(await page.locator('#agent-input').inputValue(),draft);
  const api=config.base+'/api/projects/'+config.project+'/agent';
  const data=await (await context.request.get(api)).json();const turn=data.turns.find(t=>t.id===config.task);
  assert.equal(turn.agent_profile.harness,'claude_code');assert.equal(turn.harness_switch.state,'activated');
  const foreign=await browser.newContext();assert.equal((await foreign.request.get(api+'/profiles')).status(),404);await foreign.close();
  await page.reload();await panel.locator('summary').click();await panel.getByText(/后续 Agent 执行已切换/).waitFor();
  await page.screenshot({path:config.output+'/switch-activated.png',fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(config.output+'/report.json',JSON.stringify({passed:true,actual_browser:true,actual_api_database_cache:true,
    scenarios:['profile selection','pending switch','withdraw','safe activation','input retained','reload persistence','foreign owner rejected'],
    page_errors:errors,scope:'Two API processes, real PG and Redis, S3 HTTP fixture; no Agent executor/model runs in this browser scenario.'},null,2));
  console.log(JSON.stringify({passed:true,scenarios:7}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
