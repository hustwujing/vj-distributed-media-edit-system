const {chromium}=require('/data/wujing03/node_modules/playwright'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const [base,root]=process.argv.slice(2),save=(n,v)=>fs.writeFileSync(path.join(root,n+'.json'),JSON.stringify(v,null,2)+'\n');
const full='我现在检查 BiRefNet 产出的实际前景图和蒙版，重点核对主体完整性、透明背景及边缘黑边；随后会提交结构化验收结论。';
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox']});let page;const errors=[];
try{const context=await browser.newContext({viewport:{width:1400,height:1100}});page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.addInitScript(()=>{
 window.__events=[];window.__dom=[];window.__actors=new Set();const Native=window.EventSource;
 window.EventSource=class extends Native{constructor(...args){super(...args);for(const name of ['turn','text','text_snapshot','activity','done'])this.addEventListener(name,e=>{let data;try{data=JSON.parse(e.data)}catch{}window.__events.push({at:Date.now(),event:name,data});});}};
 window.addEventListener('DOMContentLoaded',()=>{const holder=document.querySelector('#agent-messages');if(!holder)return;new MutationObserver(()=>{const text=[...holder.querySelectorAll('.assistant-text')].at(-1)?.innerText||'';if(window.__dom.at(-1)?.text!==text)window.__dom.push({at:Date.now(),text});for(const item of holder.querySelectorAll('.live-work:not([hidden]) strong'))window.__actors.add(item.innerText);}).observe(holder,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['hidden']});});
});await page.goto(base+'/');
 async function get(url){const r=await page.request.get(base+url);assert.ok(r.ok());return r.json();}
 async function project(label){const r=await page.request.post(base+'/api/projects',{data:{name:'ISOLATED-'+label}});assert.ok(r.ok());return (await r.json()).id;}
 async function chat(p){await page.goto(base+'/chat/'+p);await page.waitForFunction(()=>document.querySelector('#agent-status')?.classList.contains('online'));}
 async function send(text){const pending=page.waitForResponse(r=>r.url().endsWith('/agent/messages')&&r.request().method()==='POST');await page.locator('#agent-input').fill(text);await page.locator('#agent-send').click();const r=await pending;assert.ok(r.ok(),await r.text());return (await r.json()).id;}
 async function terminal(p,t,label){let last='',captured=false;for(let i=0;i<900;i++){const row=(await get(`/api/projects/${p}/agent`)).turns.find(x=>x.id===t);const sig=JSON.stringify([row.status,row.routing]);if(last!==sig){console.log(label,sig);last=sig;}if(label==='live-analysis'&&!captured&&(await page.locator('.live-work').last().innerText()).includes('沙盒 Agent')){await page.screenshot({path:path.join(root,'live-agent-active.png'),fullPage:true});captured=true;}if(['succeeded','failed','cancelled','paused'].includes(row.status)){save(label+'-turn',row);assert.equal(row.status,'succeeded',row.error);return row;}await page.waitForTimeout(500);}throw Error('Timeout '+label);}
 for(const transport of ['redis','database']){
  const p=await project(transport);await chat(p);const task=await send('验证文字显示。');fs.writeFileSync(path.join(root,'control.json'),JSON.stringify({action:transport,task}));
  await page.waitForFunction(text=>[...document.querySelectorAll('.assistant-text')].at(-1)?.innerText===text,full,{timeout:15000});
  const at=Date.now(),row=(await get(`/api/projects/${p}/agent`)).turns.find(x=>x.id===task);assert.equal(row.status,'running','Text completion must be visible before the task finishes');
  assert.equal(await page.locator('.assistant-text.streaming-text').count(),0);assert.ok(await page.locator('.live-work:not([hidden])').count());
  await page.screenshot({path:path.join(root,transport+'-tail-and-actor.png'),fullPage:true});await terminal(p,task,transport);await page.waitForTimeout(500);
  const trace=await page.evaluate(()=>({events:window.__events,dom:window.__dom,actors:[...window.__actors]}));
  if(transport==='redis')assert.ok(trace.events.some(e=>e.event==='text_snapshot'));
  else assert.ok(trace.events.some(e=>e.event==='turn'&&e.data.stream_transport==='database'));
  await page.reload();await page.waitForFunction(text=>document.querySelector('.assistant-text')?.innerText.includes(text),full);assert.match(await page.locator('.assistant-text').innerText(),/最终检查通过/);save(transport+'-browser',{passed:true,task,shown_at:at,retained_after_reload:true,...trace});
 }
 fs.writeFileSync(path.join(root,'control.json'),JSON.stringify({action:'live'}));await page.waitForTimeout(600);
 const p=await project('real-model');await chat(p);let t=await send('你好啊');const greeting=await terminal(p,t,'live-greeting');assert.equal(greeting.routing.stage,'light');await chat(p);
 t=await send('请核实当前系统实际有没有字幕转双语和导出剪辑工程的能力。需要查阅实际工具说明后再回答，不要凭常识猜，也不要开始制作。');
 const row=await terminal(p,t,'live-analysis');assert.equal(row.mode,'plan');assert.equal(row.routing.capability,'general_analysis');assert.equal(row.plan_readiness.can_execute,false);
 await page.waitForTimeout(600);const trace=await page.evaluate(()=>({events:window.__events,dom:window.__dom,actors:[...window.__actors]}));assert.ok(trace.events.some(e=>e.event==='text'));assert.ok(trace.actors.includes('沙盒 Agent'));assert.ok(trace.actors.includes('轻量 Agent'));assert.equal(await page.locator('#agent-confirmation').isVisible(),false);assert.equal(await page.locator('.assistant-text.streaming-text').count(),0);
 const usage=await get(`/api/projects/${p}/tasks/${t}/usage?limit=200`);assert.equal(usage.provider.submissions,0);save('live-analysis-usage',usage);save('live-browser',{passed:true,task:t,...trace});await page.screenshot({path:path.join(root,'live-final.png'),fullPage:true});

 const media=await project('media-workflow');
 for(const [name,mimeType] of [['clip.mp4','video/mp4'],['logo.png','image/png']]){const upload=await page.request.post(base+`/api/projects/${media}/assets`,{multipart:{file:{name,mimeType,buffer:fs.readFileSync(path.join(root,name))}}});assert.ok(upload.ok(),await upload.text());}
 await chat(media);const plan=await send('把我上传图标的纯黑背景改为透明，再贴到上传视频右上角，距边缘10像素，保持视频原来的时长、尺寸和帧率，不需要声音。只用本地处理，不调用图片生成服务。先给方案和一张静态效果预览即可，不做反复精修；确认后用后台工作流分步生成透明图标、合成完整视频并检查。');
 let planned=await terminal(media,plan,'media-plan');
 for(let retry=0;!planned.plan_readiness.can_execute&&planned.plan_readiness.retry_available&&retry<2;retry++){await page.locator('[data-review-retry]').last().click();await page.waitForTimeout(1000);for(let i=0;i<40;i++){planned=(await get(`/api/projects/${media}/agent`)).turns.find(x=>x.id===plan);if(planned.plan_readiness.state!=='pending')break;await page.waitForTimeout(500);}}
 assert.equal(planned.plan_readiness.can_execute,true,JSON.stringify(planned.plan_readiness));assert.equal(planned.execution_intent.route,'workflow');
 await page.waitForFunction(()=>!document.querySelector('#agent-confirmation').hidden);const confirmation=page.waitForResponse(r=>r.url().endsWith('/agent/messages')&&r.request().method()==='POST');await page.locator('#agent-execute').click();const execute=(await (await confirmation).json()).id;
 const delivered=await terminal(media,execute,'media-execute');assert.ok(delivered.workflow?.steps.length>=2);assert.ok(delivered.workflow.steps.every(x=>x.status==='succeeded'));
 const video=delivered.artifacts.find(x=>x.type?.startsWith('video/'));assert.ok(video);const download=await page.request.get(base+'/'+video.url);assert.ok(download.ok());fs.writeFileSync(path.join(root,'delivered.mp4'),await download.body());
 const consumed=await get(`/api/projects/${media}/tasks/${execute}/usage?scope=run&limit=200`);assert.ok(consumed.counts.workflow.calls>=2);assert.equal(consumed.counts.provider_submit.calls,0);save('media-usage',consumed);save('media-state',{project:media,plan,execute});
 await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.assistant-text').length>=2);const all=await page.locator('.assistant-text').allInnerTexts();for(const message of delivered.messages||[])if(message.status==='completed'&&message.text.length<200)assert.ok(all.at(-1).includes(message.text.replace(/\*\*/g,''))||message.text.includes('sandbox:'));
 save('media-result',{passed:true,steps:delivered.workflow.steps.length,artifact_bytes:(await download.body()).length});
 assert.deepEqual(errors,[]);save('isolated-browser-result',{passed:true,fixtures:2,real_model_tasks:2,page_errors:errors});console.log('All browser cases passed');
}catch(e){save('isolated-browser-result',{passed:false,error:e.message,page_errors:errors});if(page)await page.screenshot({path:path.join(root,'isolated-failure.png'),fullPage:true}).catch(()=>{});console.error(e.stack);process.exitCode=1;}finally{await browser.close();}})();
