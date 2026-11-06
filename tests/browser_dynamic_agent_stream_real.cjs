const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const base=process.argv[2]||'http://127.0.0.1/distributed-agent-system',out=path.resolve('.cluster/ui-review/agent-stream-real');
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,args:['--no-sandbox']});const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();let project,runId;const errors=[];page.on('pageerror',error=>errors.push(error.message));
 try{
  const created=await context.request.post(base+'/api/projects',{data:{name:'Real public agent stream check'}});assert.equal(created.status(),201);project=(await created.json()).id;
  await page.goto(`${base}/chat/${project}`,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!document.querySelector('#dynamic-model-profile')?.disabled);
  await page.locator('#dynamic-input-content').fill('创建 report.json，内容必须是 {"status":"ok","source":"sandbox"}。必须由 editing_implementer 在本地沙盒中生成并验证，不调用任何外部媒体 Provider。');
  await page.locator('#dynamic-execution-settings').evaluate(node=>node.open=true);await page.locator('[name="delivery-target"]').evaluateAll(nodes=>nodes.forEach(node=>{node.checked=false;node.dispatchEvent(new Event('change',{bubbles:true}));}));await page.locator('[name="delivery-target"][value="报告 JSON"]').check();
  await page.locator('#dynamic-scope').evaluate(node=>node.open=true);await page.locator('[data-operation]').evaluateAll(nodes=>nodes.forEach(node=>{node.value='0';node.dispatchEvent(new Event('input',{bubbles:true}));}));await page.locator('#dynamic-token-budget').fill('400000');await page.locator('#dynamic-time').fill('20');await page.locator('#dynamic-input-submit').click();
  await page.waitForFunction(()=>document.querySelector('#dynamic-current')?.dataset.runId,null,{timeout:60000});runId=await page.locator('#dynamic-current').getAttribute('data-run-id');
  await page.waitForFunction(()=>document.querySelector('#dynamic-agent-text')?.textContent.includes('Manager：'),null,{timeout:300000});
  await page.waitForFunction(()=>document.querySelector('#dynamic-agent-text')?.textContent.includes('Implementer：'),null,{timeout:600000});
  const text=await page.locator('#dynamic-agent-text').innerText();assert.match(text,/Manager：/);assert.match(text,/Implementer：/);assert.doesNotMatch(text,/PRIVATE|reasoning|chain.of.thought/i);assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(out,'public-agent-stream.png'),fullPage:true});fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({passed:true,project,run_id:runId,text,release:JSON.parse(fs.readFileSync('.cluster/release.json'))},null,2)+'\n',{mode:0o600});
 }finally{
  if(project&&runId){await context.request.post(`${base}/api/projects/${project}/runs/${runId}/commands`,{data:{request_id:crypto.randomBytes(16).toString('hex'),action:'cancel'}});const end=Date.now()+120000;while(Date.now()<end){const response=await context.request.get(`${base}/api/projects/${project}/runs/${runId}`);if(response.ok()&&['SUCCEEDED','FAILED','CANCELLED'].includes((await response.json()).state))break;await page.waitForTimeout(500);}}
  if(project)await context.request.delete(`${base}/api/projects/${project}`);await browser.close();
 }
 console.log(JSON.stringify({passed:true,project,run_id:runId}));
})().catch(error=>{console.error(error.stack||error.message);process.exitCode=1;});
