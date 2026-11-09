const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright'),root=path.resolve(__dirname,'../webapp/static');
const server=http.createServer((req,res)=>{const name=new URL(req.url,'http://localhost').pathname;
 if(name==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><base href="/"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/studio.css"><link rel="stylesheet" href="/dynamic.css"><main id="confirmation"></main>');return;}
 if(!/^\/[a-z_]+\.(js|css)$/.test(name)){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(path.join(root,name)));});
(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;const results=[];
 try{browser=await chromium.launch({headless:true,args:['--no-sandbox']});for(const width of [1440,390]){
  const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('http://127.0.0.1:'+server.address().port+'/');
  await page.addScriptTag({type:'module',content:`import {confirmationDetails} from '/dynamic_results.js';
   const request={delivery_scope_changed:true,previous_delivery_targets:['old.mp4','JSON报告'],delivery_targets:['new.mp4'],delivery_changes:{filenames:{removed:['old.mp4'],added:['new.mp4']},formats:{removed:['json'],added:[]},roles:{removed:['report'],added:[]}},steps:[{name:'视频处理',objective:'保留其余变速要求',parameters:{width:640,height:360,fps:24}}]};
   document.querySelector('#confirmation').innerHTML=confirmationDetails({kind:'plan_confirmation',request});`});
  await page.locator('.delivery-amendment').waitFor();const text=await page.locator('#confirmation').innerText();for(const value of ['原清单','old.mp4','新清单','new.mp4','移除文件格式：json','移除交付角色：report','未修改的内容要求及验收标准继续保留'])assert.ok(text.includes(value),value);
  await page.getByText('关键参数',{exact:true}).click();assert.match(await page.locator('pre').innerText(),/"width": 640/);assert.match(await page.locator('pre').innerText(),/"fps": 24/);assert.deepEqual(errors,[]);results.push({width,passed:true,old_new_scope_and_removed_requirements_visible:true,current_parameters_visible:true});await page.close();
 }const report={passed:true,results};if(process.argv[2])fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));}
 finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
