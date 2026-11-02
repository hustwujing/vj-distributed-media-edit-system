const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const [base,project,cookies,directory]=process.argv.slice(2);const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
try{const context=await browser.newContext({viewport:{width:1440,height:1000}});await context.addCookies(JSON.parse(fs.readFileSync(cookies)));const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto(base+'/chat/'+project);await page.locator('#agent-input').waitFor();await page.reload();await page.locator('#agent-input').waitFor();
const response=await page.request.get(base+'/api/projects/'+project+'/agent');assert.ok(response.ok());const turns=(await response.json()).turns;assert.ok(turns.some(t=>t.workflow?.steps.some(s=>s.reuse_from)));
await page.goto(base+'/');await page.goto(base+'/chat/'+project);await page.locator('#agent-input').waitFor();await page.screenshot({path:path.join(directory,'reentered.png'),fullPage:true});assert.deepEqual(errors,[]);
fs.writeFileSync(path.join(directory,'browser-reconnect.json'),JSON.stringify({passed:true,page_errors:errors,turns:turns.map(t=>({id:t.id,status:t.status}))},null,2));
}finally{await browser.close();}})().catch(error=>{console.error(error.message);process.exitCode=1;});
