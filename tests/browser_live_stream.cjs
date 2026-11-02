const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('fs'),assert=require('assert/strict');
const config=JSON.parse(fs.readFileSync(process.argv[2]));
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const context=await browser.newContext({viewport:{width:1400,height:1000}});await context.addCookies(config.cookies);
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(config.base+'/chat/'+config.project);
  const selector=`[data-turn="${config.task}"] .assistant-text`;
  await page.locator(selector).waitFor({state:'attached'});
  await page.evaluate(selector=>{
   const target=document.querySelector(selector);window.streamSamples=[];let previous='';
   new MutationObserver(()=>{const text=target.textContent;if(text&&text!==previous){previous=text;window.streamSamples.push({time:Date.now(),text,streaming:target.classList.contains('streaming-text')});}}).observe(target,{subtree:true,childList:true,characterData:true});
  },selector);
  fs.writeFileSync(config.output+'/browser-ready','1');
  const deadline=Date.now()+330000;
  while(!fs.existsSync(config.output+'/browser-done.json')){
   if(Date.now()>deadline)throw Error('Timed out waiting for real model completion');
   await page.waitForTimeout(200);
  }
  await page.locator(selector).filter({hasText:'流式输出检查结束。'}).waitFor({timeout:10000});
  await page.waitForTimeout(500);
  const samples=await page.evaluate(()=>window.streamSamples),text=await page.locator(selector).innerText();
  const expected=JSON.parse(fs.readFileSync(config.output+'/browser-done.json')).text;
  const normalize=value=>value.replace(/\s+/g,'');
  assert.ok(normalize(text).includes(normalize(expected)),'Visible transcript must contain complete canonical reply including tail');
  const partial=samples.filter(s=>s.streaming&&s.text.length>0&&s.text.length<expected.length);
  assert.ok(partial.length>=2,'Browser must render multiple partial updates before completion');
  await page.screenshot({path:config.output+'/browser-stream-final.png',fullPage:true});
  await page.reload();await page.locator(selector).filter({hasText:'流式输出检查结束。'}).waitFor();
  assert.ok(normalize(await page.locator(selector).innerText()).includes(normalize(expected)));
  assert.deepEqual(errors,[]);
  const report={passed:true,partial_dom_updates:partial.length,total_dom_updates:samples.length,
    first_dom_update_at:samples[0]?.time,last_dom_update_at:samples.at(-1)?.time,
    complete_text_and_tail_present:true,reload_preserves_text:true,page_errors:errors};
  fs.writeFileSync(config.output+'/browser-stream-report.json',JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1});
