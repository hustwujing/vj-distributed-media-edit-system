/* Eight real browser sessions must load the public gallery without broken posters. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');

const base=process.env.DEMO_BURST_BASE||'http://updream-lab.bilibili.co/distributed-agent-sandbox-backend';
const output=path.resolve(process.argv[2]||'');
assert.ok(process.argv[2]&&output.startsWith('/data2/wujing03/'),'Evidence must stay on /data2/wujing03');
fs.mkdirSync(output,{recursive:true,mode:0o700});

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 const results=[];
 try{
  await Promise.all(Array.from({length:8},async(_,index)=>{
   const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
   const responseErrors=[],pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
   page.on('response',response=>{const url=new URL(response.url());if(url.pathname.includes('/api/demos/')&&url.pathname.includes('/files/')&&response.status()>=400)responseErrors.push({status:response.status(),path:url.pathname});});
   await page.goto(base+'/',{waitUntil:'domcontentloaded',timeout:60000});
   await page.waitForFunction(()=>document.querySelectorAll('.demo-card').length>=70,null,{timeout:60000});
   const height=await documentHeight(page);for(let y=0;y<height;y+=700){await page.evaluate(value=>scrollTo(0,value),y);await page.waitForTimeout(80);}
   await page.waitForFunction(()=>[...document.querySelectorAll('img[data-poster]')].every(image=>image.dataset.posterState==='loaded'),null,{timeout:90000});
   const posters=await page.locator('img[data-poster]').evaluateAll(images=>images.map(image=>({state:image.dataset.posterState,width:image.naturalWidth,height:image.naturalHeight})));
   assert.ok(posters.length>=70);assert.ok(posters.every(item=>item.state==='loaded'&&item.width>0&&item.height>0));
   assert.deepEqual(responseErrors,[]);assert.deepEqual(pageErrors,[]);
   if(index===0)await page.screenshot({path:path.join(output,'gallery.png'),fullPage:true});
   results.push({index,posters:posters.length,response_errors:responseErrors,page_errors:pageErrors});await context.close();
  }));
  const value={passed:true,sessions:results.length,posters:results.reduce((sum,item)=>sum+item.posters,0),results};
  fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(value,null,2)+'\n',{mode:0o600});console.log(JSON.stringify(value));
 }finally{await browser.close();}
})().catch(error=>{fs.writeFileSync(path.join(output,'result.json'),JSON.stringify({passed:false,error:error.stack},null,2)+'\n',{mode:0o600});console.error(error.stack);process.exitCode=1;});

function documentHeight(page){return page.evaluate(()=>document.documentElement.scrollHeight);}
