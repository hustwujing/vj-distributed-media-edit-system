/* Browser model selection and a budget-denied Run; no paid model generation. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const repo=path.resolve(__dirname,'..');
const out=path.join(repo,'.cluster/deployment-validation/token-estimation');
const base='http://127.0.0.1/distributed-agent-system';
const save=value=>fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify(value,null,2)+'\n',{mode:0o600});
(async()=>{
 fs.mkdirSync(out,{recursive:true,mode:0o700});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 const result={started_at:Date.now()/1000,base,release:JSON.parse(fs.readFileSync(path.join(repo,'.cluster/release.json'))),page_errors:[],business_runs_created:0};
 page.on('pageerror',e=>result.page_errors.push(e.message));let project,runId;
 try{
  const response=await page.goto(base+'/',{waitUntil:'domcontentloaded'});assert.equal(response.status(),200);
  await page.locator('#new-project').click();await page.locator('#new-project-form input[name="name"]').fill('Model configuration deployment check');
  await page.locator('#new-project-form .mode-choices label').filter({has:page.locator('input[value="chat"]')}).click();
  await page.locator('#new-project-form button[type="submit"]').click();await page.waitForURL(/\/chat\/[a-f0-9]{32}/);
  project=await page.locator('body').getAttribute('data-project');assert.match(project,/^[a-f0-9]{32}$/);result.project=project;
  await page.waitForFunction(()=>document.querySelector('#dynamic-connection')?.textContent.includes('同步'));
  assert.equal(await page.locator('#dynamic-model-profile').inputValue(),'claude');
  assert.equal(await page.locator('#dynamic-reasoning-effort').inputValue(),'high');
  await page.screenshot({path:path.join(out,'default-model.png'),fullPage:true});
  const profiles=await context.request.get(`${base}/api/projects/${project}/agent/profiles`);assert.equal(profiles.status(),200);
  const configuration=await profiles.json();result.model_profiles=configuration.profiles;
  result.selected_profile=configuration.selected;assert.equal(result.selected_profile,'claude');
  for(const [name,model,policy] of [['claude','claude-4.6-sonnet','claude_bpe_estimate_v1'],['claude5','claude-opus-5','claude_bpe_estimate_v1'],['deepseek','deepseek-v4-flash','deepseek_bpe_estimate_v1'],['deepseek41','deepseek-v4.1-flash','deepseek_bpe_estimate_v1']]){
   const p=result.model_profiles.find(p=>p.name===name);assert.ok(p?.available);assert.equal(p.model,model);assert.equal(p.input_token_policy,policy);
   if(name==='claude')assert.equal(p.reasoning_effort,'high');
   const selected=await context.request.put(`${base}/api/projects/${project}/agent/profile`,{data:{profile:name}});assert.equal(selected.status(),200);assert.equal((await selected.json()).model,model);
  }
  const cap=await context.request.get(`${base}/api/projects/${project}/runs/capabilities`);assert.equal(cap.status(),200);
  const capabilities=await cap.json();assert.ok(capabilities.workers_ready);
  result.role_harnesses=[...new Set(capabilities.profiles.map(p=>p.harness))];assert.deepEqual(result.role_harnesses,['structured-profile-v3']);
  assert.equal(capabilities.model_protocol_policy,'native_commands_strict_preferred');
  const runs=await context.request.get(`${base}/api/projects/${project}/runs`);assert.equal(runs.status(),200);assert.equal((await runs.json()).runs.length,0);
  await page.locator('#dynamic-model-profile').selectOption('deepseek41');
  await page.locator('#dynamic-reasoning-effort').selectOption('low');
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>document.querySelector('#dynamic-connection')?.textContent.includes('同步'));
  assert.equal(await page.locator('#dynamic-model-profile').inputValue(),'deepseek41');
  assert.equal(await page.locator('#dynamic-reasoning-effort').inputValue(),'low');
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.locator('#dynamic-model-profile').isVisible());
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  await page.screenshot({path:path.join(out,'model-selection-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#dynamic-goal').fill('部署验证：检查模型选择和预算拦截，不执行任何媒体操作。');
  await page.locator('#dynamic-targets').fill('验证记录');
  await page.locator('#dynamic-scope').evaluate(node=>{node.open=true;});
  await page.locator('#dynamic-token-budget').fill('1024');
  const submitted=page.waitForResponse(r=>r.url().endsWith(`/api/projects/${project}/runs`)&&r.request().method()==='POST');
  await page.locator('#dynamic-submit').click();const responseRun=await submitted;assert.equal(responseRun.status(),202,await responseRun.text());
  const run=await responseRun.json();runId=run.id;result.business_runs_created=1;result.run_id=runId;
  result.submitted_choice=responseRun.request().postDataJSON().model_choice;result.frozen_model=run.model_configuration;
  assert.equal(result.frozen_model.model,'deepseek-v4.1-flash');assert.equal(result.frozen_model.reasoning_effort,'low');
  const end=Date.now()+90000;let state;
  while(Date.now()<end){state=await (await context.request.get(`${base}/api/projects/${project}/runs/${runId}`)).json();if(state.terminal_reason==='budget_exhausted')break;await page.waitForTimeout(500);}
  assert.equal(state.terminal_reason,'budget_exhausted');
  const usage=await (await context.request.get(`${base}/api/projects/${project}/runs/${runId}/usage`)).json();
  result.generated_model_calls=usage.counts.llm.calls;assert.equal(result.generated_model_calls,0);
  const changedDefault=await context.request.put(`${base}/api/projects/${project}/agent/profile`,{data:{profile:'claude'}});assert.equal(changedDefault.status(),200);
  const unchanged=await (await context.request.get(`${base}/api/projects/${project}/runs/${runId}`)).json();assert.deepEqual(unchanged.model_configuration,result.frozen_model);
  await page.reload({waitUntil:'domcontentloaded'});await page.locator('#dynamic-model-current').filter({hasText:'deepseek-v4.1-flash'}).waitFor();
  assert.match(await page.locator('#dynamic-model-current').textContent(),/推理强度：低/);
  await page.screenshot({path:path.join(out,'frozen-model-run.png'),fullPage:true});
  assert.deepEqual(result.page_errors,[]);result.passed=true;
 }catch(error){result.passed=false;result.error=error.message;throw error;}
 finally{
  if(project&&runId){
   await context.request.post(`${base}/api/projects/${project}/runs/${runId}/commands`,{data:{request_id:require('node:crypto').randomBytes(16).toString('hex'),action:'cancel'}});
   const end=Date.now()+90000;
   while(Date.now()<end){const run=await (await context.request.get(`${base}/api/projects/${project}/runs/${runId}`)).json();if(['CANCELLED','FAILED','SUCCEEDED'].includes(run.state))break;await page.waitForTimeout(500);}
  }
  if(project){const response=await context.request.delete(`${base}/api/projects/${project}`);result.cleanup_status=response.status();if(!response.ok())result.passed=false;}
  result.finished_at=Date.now()/1000;save(result);await browser.close();
 }
 if(!result.passed)throw Error('Deployment browser check failed');
 console.log(JSON.stringify({passed:result.passed,profiles:result.model_profiles.map(p=>({name:p.name,model:p.model,input_token_policy:p.input_token_policy})),harnesses:result.role_harnesses,cleanup_status:result.cleanup_status}));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
