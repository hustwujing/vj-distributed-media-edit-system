// Real browser verification for answer-only terminal runs. No semantic verdict.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const normalize=value=>value.replace(/\s+/g,' ').trim();
async function verifyAnswerView(page,run,capture,countStreams){
 assert.equal(run.state,'SUCCEEDED');assert.equal(run.terminal_reason,'conversation_answer');
 assert.ok(run.conversation_answer?.trim(),'Answer-only result is empty');
 assert.equal(run.tasks.length,0,'A textual reply cannot bypass existing business work');
 assert.equal(run.delivery.length,0);
 await page.getByRole('heading',{name:'答复',exact:true}).waitFor();
 assert.ok(normalize(await page.locator('#dynamic-run-result').innerText()).includes(normalize(run.conversation_answer)),'Visible answer differs from committed result');
 const before=countStreams();await page.waitForTimeout(5000);assert.equal(countStreams(),before,'Terminal answer SSE keeps reconnecting');
 await capture('answer-completed');await page.reload();await page.getByRole('heading',{name:'答复',exact:true}).waitFor();
 assert.ok(normalize(await page.locator('#dynamic-run-result').innerText()).includes(normalize(run.conversation_answer)));
 await capture('answer-reentered');
}
module.exports={verifyAnswerView};
if(require.main===module){
 const {chromium}=require('playwright');
 (async()=>{const directory=path.resolve(process.argv[2]||'');assert.ok(process.argv[2]&&directory.startsWith('/data2/wujing03/'));
 const state=JSON.parse(fs.readFileSync(path.join(directory,'state.json'))),base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend';
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 try{const context=await browser.newContext({storageState:path.join(directory,'browser-state.json'),viewport:{width:1440,height:1000}}),page=await context.newPage(),errors=[];let streams=0;
 page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.url().endsWith('/stream'))streams++});
 const cookie=(await context.cookies(base+'/')).map(c=>c.name+'='+c.value).join('; ');
 const response=await context.request.get(`http://127.0.0.1/distributed-agent-sandbox/api/projects/${state.project}/runs/${state.run_id}`,{headers:{Cookie:cookie}});assert.ok(response.ok());const run=await response.json();
 await page.goto(base+'/chat/'+state.project);await verifyAnswerView(page,run,name=>page.screenshot({path:path.join(directory,name+'.png'),fullPage:true}),()=>streams);
 assert.equal(run.cleanup_pending,0);assert.deepEqual(errors,[]);
 const record={browser_verified:true,semantic_review_required:true,run_id:run.id,state:run.state,reason:run.terminal_reason,cleanup_pending:run.cleanup_pending,page_errors:errors,checks:['committed_answer_visible','terminal_sse_stable','answer_survives_reentry']};
 fs.writeFileSync(path.join(directory,'answer-browser-review.json'),JSON.stringify(record,null,2)+'\n',{mode:0o600});console.log(JSON.stringify(record));
 }finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1});
}
