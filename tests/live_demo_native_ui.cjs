// Real isolated native pages; no provider calls or production project writes.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const campaign=path.resolve(process.argv[2]||''),output=path.resolve(process.argv[3]||'');
assert.ok(process.argv[2]&&process.argv[3]&&campaign.startsWith('/data2/wujing03/')&&output.startsWith(campaign+'/'));
const selected=process.argv[4]?new Set(process.argv[4].split(',')):null;
const release=JSON.parse(fs.readFileSync(path.join(__dirname,'../.sandbox-cluster/release.json')));
assert.equal(release.namespace,'sandbox-agent');assert.equal(release.state,'ready');
const catalog=JSON.parse(fs.readFileSync(path.join(campaign,'catalog-original.json'))),latest=new Map();
for(const round of fs.readdirSync(campaign,{withFileTypes:true}).filter(x=>x.isDirectory())){
 for(const item of fs.readdirSync(path.join(campaign,round.name),{withFileTypes:true}).filter(x=>x.isDirectory())){
  const dir=path.join(campaign,round.name,item.name),file=path.join(dir,'state.json');if(!fs.existsSync(file))continue;
  const state=JSON.parse(fs.readFileSync(file));if(!state.run_id||!state.finished_at)continue;
  if(!latest.has(state.case)||state.started_at>latest.get(state.case).state.started_at)latest.set(state.case,{dir,state});
 }
}
const save=(file,data)=>{fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});fs.writeFileSync(file,JSON.stringify(data,null,2)+'\n',{mode:0o600});};
const base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend',results=[];
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 let cursor=0;const cases=catalog.filter(item=>!selected||selected.has(item.id));
 try{await Promise.all(Array.from({length:Math.min(3,cases.length)},async()=>{while(cursor<cases.length){
  const demo=cases[cursor++],prior=latest.get(demo.id);assert.ok(prior,'No finished own project for '+demo.id);
  const dir=path.join(output,demo.id),record={case:demo.id,project:prior.state.project,release_sha:release.sha256,checks:[],page_errors:[],started_at:Date.now()/1000};
  const context=await browser.newContext({storageState:path.join(prior.dir,'browser-state.json'),viewport:{width:1440,height:1000}}),page=await context.newPage();page.setDefaultTimeout(30000);
  page.on('pageerror',error=>record.page_errors.push(error.message));
  const api=async suffix=>{const cookie=(await context.cookies(base+'/')).map(x=>x.name+'='+x.value).join('; ');const response=await context.request.get('http://127.0.0.1/distributed-agent-sandbox/api/projects/'+record.project+suffix,{headers:{Cookie:cookie}});assert.ok(response.ok(),'Native metadata '+response.status());return response.json();};
  try{
   const mode=demo.media==='image'?'images':'tools';record.mode=mode;
   await page.goto(base+'/'+mode+'/'+record.project);
   if(mode==='images'){
    await page.waitForFunction(()=>document.body.dataset.imageReady==='true'&&Number(document.querySelector('#canvas-width')?.value)>0&&document.querySelector('#image-canvas')?.width>0);
    await page.locator('#save-document').waitFor();record.checks.push('image_canvas_and_properties_loaded');
    if(await page.locator('#ai-panel').isVisible())assert.ok(await page.locator('#image-action').inputValue(),'Visible image tool has no selected operation');
    const expected={'image-adjustment-layers':'adjustment','image-text-effects':'text','image-layout-guides':'image'}[demo.id];
    if(expected){
     const committed=page.waitForResponse(response=>response.request().method()==='PUT'&&response.url().endsWith('/image-document'));
     await page.locator('#save-document').click();const response=await committed;assert.ok(response.ok(),'Native canvas save failed');
     await page.waitForFunction(()=>/已保存/.test(document.querySelector('#document-status')?.textContent||''));
     const saved=await api('/image-document');record.layer_kinds=saved.document.layers.map(x=>x.kind);
     assert.ok(record.layer_kinds.includes(expected));if(demo.id==='image-layout-guides')assert.equal(record.layer_kinds.filter(x=>x==='image').length,3);
     assert.ok(saved.document.revision>0);await page.reload();await page.waitForFunction(()=>document.body.dataset.imageReady==='true');
     const restored=await api('/image-document');assert.equal(restored.document.revision,saved.document.revision);assert.deepEqual(restored.document.layers,saved.document.layers);
     record.checks.push('native_editable_structure_saved_and_reloaded');
    }
   }else{
    await page.locator('.settings-heading h2').waitFor();await page.waitForFunction(()=>document.body.classList.contains('demo-project'));
    record.checks.push('native_tool_parameters_loaded');
    if(demo.id==='video-caption-refine'){
     const project=await api('');await page.locator('#caption-load select').selectOption(project.assets.find(x=>x.kind==='video').id);
     await page.locator('#caption-load button').click();await page.locator('#caption-save .caption-cue').first().waitFor();
     record.caption_cues=await page.locator('#caption-save .caption-cue').count();assert.ok(record.caption_cues>0);record.checks.push('existing_transcript_loaded_in_caption_editor');
    }
    if(demo.id==='video-version-compare'){
     await page.getByRole('button',{name:'打开同步对比',exact:true}).click();
     await page.waitForFunction(()=>[...document.querySelectorAll('dialog.video-compare video')].length===2&&[...document.querySelectorAll('dialog.video-compare video')].every(x=>x.readyState>=1));
     await page.locator('dialog.video-compare [data-play]').click();
     await page.waitForFunction(()=>[...document.querySelectorAll('dialog.video-compare video')].every(x=>!x.paused&&x.currentTime>.2));
     await page.locator('dialog.video-compare [data-play]').click();
     const times=await page.locator('dialog.video-compare video').evaluateAll(v=>v.map(x=>x.currentTime));assert.ok(Math.abs(times[0]-times[1])<.3);
     await page.locator('dialog.video-compare header button').click();record.checks.push('two_real_video_sources_play_in_sync');
    }
   }
   assert.deepEqual(record.page_errors,[]);record.passed=true;fs.mkdirSync(dir,{recursive:true,mode:0o700});await page.screenshot({path:path.join(dir,'native.png'),fullPage:true});
  }catch(error){record.passed=false;record.error=error.message;fs.mkdirSync(dir,{recursive:true,mode:0o700});await page.screenshot({path:path.join(dir,'failed.png'),fullPage:true}).catch(()=>{});}
  finally{record.seconds=Date.now()/1000-record.started_at;save(path.join(dir,'report.json'),record);results.push(record);console.log(JSON.stringify({case:demo.id,passed:record.passed,error:record.error}));await context.close();}
 }}));}finally{await browser.close();save(path.join(output,'summary.json'),{total:cases.length,passed:results.filter(x=>x.passed).length,results});}
 if(results.some(x=>!x.passed))process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
