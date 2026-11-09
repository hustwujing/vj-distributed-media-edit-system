// Real result buttons + shared preview renderer; local media, no model calls.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../webapp/static'),source=fs.readFileSync(path.join(root,'dynamic.js'),'utf8');
const show=source.slice(source.indexOf('function showArtifact('),source.indexOf('function modelControls('));
const video=fs.readFileSync(process.env.PREVIEW_VIDEO_FIXTURE);
const wav=Buffer.alloc(44+1600);wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(1600,40);
const files={
 '/clip.mp4':['video/mp4',video],'/sound.wav':['audio/wav',wav],
 '/picture.svg':['image/svg+xml',Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="blue"/></svg>')],
 '/data.json':['application/json',Buffer.from('{"message":"预览内容"}')],
 '/captions.srt':['text/plain',Buffer.from('1\n00:00:00,000 --> 00:00:01,000\n你好')],
 '/unknown.bin':['application/octet-stream',Buffer.from([0,255,1])],
 '/broken.mp4':['video/mp4',Buffer.from('not a video')],
};
const server=http.createServer((req,res)=>{
 const name=new URL(req.url,'http://localhost').pathname;
 if(name==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><base href="/"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/studio.css"><div id="results"></div><div id="dynamic-preview" style="margin-top:2000px"></div>');return;}
 if(files[name]){const [type,data]=files[name];res.setHeader('Content-Type',type);const range=req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);if(range){const start=Number(range[1]),end=Math.min(data.length-1,range[2]?Number(range[2]):data.length-1);res.writeHead(206,{'Content-Range':`bytes ${start}-${end}/${data.length}`,'Content-Length':end-start+1});res.end(data.subarray(start,end+1));}else{res.setHeader('Content-Length',data.length);res.end(data);}return;}
 if(/^\/[a-z_]+\.(js|css)$/.test(name)){res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(path.join(root,name)));return;}
 res.writeHead(404);res.end();
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;const results=[];
 try{
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.goto('http://127.0.0.1:'+server.address().port+'/');
   await page.addScriptTag({type:'module',content:`import {$,Preview,openMedia} from '/common.js';import {renderRunResult} from '/dynamic_results.js';${show}
    const names=${JSON.stringify(Object.keys(files).map(n=>n.slice(1)))},delivery=names.map(name=>({name,url:name,type:'application/octet-stream'}));
    window.inline=new Preview($('dynamic-preview'));await window.inline.show(delivery[0]);
    renderRunResult($('results'),{id:'done',state:'SUCCEEDED',delivery,tasks:[]},showArtifact);window.ready=true;`});
   await page.waitForFunction(()=>window.ready);
   const cases=[['clip.mp4','video'],['picture.svg','img'],['sound.wav','audio'],['data.json','pre'],['captions.srt','pre'],['unknown.bin','.preview-empty'],['broken.mp4','.preview-error']];
   for(const [name,selector] of cases){
    await page.getByRole('button',{name:'预览 '+name,exact:true}).click();const dialog=page.locator('#media-modal');await dialog.waitFor({state:'visible'});
    const item=dialog.locator(selector);await item.waitFor({state:'visible'});
    if(name==='clip.mp4'){await item.evaluate(v=>v.play());await page.waitForFunction(()=>document.querySelector('#media-modal video').currentTime>0);}
    if(name==='picture.svg')await page.waitForFunction(()=>document.querySelector('#media-modal img').naturalWidth===32);
    if(name==='data.json')assert.match(await item.innerText(),/预览内容/);
    if(name==='captions.srt')assert.match(await item.innerText(),/你好/);
    if(name==='broken.mp4')assert.match(await item.innerText(),/加载或解码失败/);
    if(name==='unknown.bin')assert.match(await item.innerText(),/暂不支持/);
    await page.getByRole('button',{name:'关闭预览',exact:true}).click();await dialog.waitFor({state:'hidden'});
    await page.waitForFunction(()=>[...document.querySelectorAll('#media-modal video,#media-modal audio')].every(n=>n.paused));
   }
   assert.deepEqual(errors,[]);results.push({width,passed:true,types:cases.map(c=>c[0]),visible_modal:true,playback_and_close:true});await page.close();
  }
  const report={passed:true,kind:'actual_result_button_and_preview_with_local_media',results};if(process.argv[2])fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
