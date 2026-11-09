// Read-only review of an existing case; never retries business execution.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto'),cp=require('node:child_process');
const {chromium}=require('playwright');
const out=path.resolve(process.argv[2]||'');assert.ok(out.startsWith('/data2/wujing03/'));
const state=JSON.parse(fs.readFileSync(path.join(out,'state.json'))),base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend',internal='http://127.0.0.1/distributed-agent-sandbox';
const save=(name,value)=>fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2)+'\n',{mode:0o600});
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']});
 try{
  const context=await browser.newContext({storageState:path.join(out,'browser-state.json'),viewport:{width:1440,height:1000}}),page=await context.newPage();
  const api=async suffix=>{const cookies=await context.cookies(base+'/');const r=await context.request.get(internal+suffix,{headers:{Cookie:cookies.map(c=>c.name+'='+c.value).join('; ')}});assert.ok(r.ok(),`${r.status()} ${suffix}`);return r;};
  const run=await(await api(`/api/projects/${state.project}/runs/${state.run_id}`)).json(),usage=await(await api(`/api/projects/${state.project}/runs/${state.run_id}/usage`)).json();save('reviewed-run.json',run);save('usage.json',usage);
  assert.equal(run.state,'SUCCEEDED');assert.equal(run.cleanup_pending,0);
  const actual=usage.harness_runs.filter(row=>row.profile_name==='editing_implementer');assert.ok(actual.length);
  const expected=state.expected_engine==='native-implementer-v1'?run.model_configuration.roles.editing_implementer.harness:state.expected_engine;assert.ok(actual.every(row=>row.harness===expected));
  const artifacts=path.join(out,'artifacts');fs.mkdirSync(artifacts,{recursive:true});
  for(const item of run.delivery){const suffix=new URL(item.url,base+'/').pathname.replace('/distributed-agent-sandbox-backend','');const bytes=await(await api(suffix)).body();assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),item.sha256);fs.writeFileSync(path.join(artifacts,path.basename(item.name||item.file)),bytes);}
  let media=null;
  if(state.case==='NH02'){
   const file=path.join(artifacts,'portrait.mp4');media=JSON.parse(cp.execFileSync('ffprobe',['-v','error','-show_entries','stream=codec_type,width,height,r_frame_rate,nb_frames:format=duration','-of','json',file]));
   const video=media.streams.find(s=>s.codec_type==='video');assert.equal(video.width,720);assert.equal(video.height,1280);assert.equal(video.r_frame_rate,'24/1');assert.ok(Math.abs(Number(media.format.duration)-9)<.1);assert.ok(media.streams.some(s=>s.codec_type==='audio'));
   cp.execFileSync('ffmpeg',['-v','error','-i',file,'-f','null','-'],{timeout:60000});
   cp.execFileSync('ffmpeg',['-v','error','-i',file,'-vf','fps=1/3,scale=180:320,tile=3x1','-frames:v','1','-y',path.join(out,'independent-frames.png')],{timeout:60000});
  }
  await page.goto(base+'/chat/'+state.project);await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();
  if(state.case==='NH02')for(const width of [1440,390]){await page.setViewportSize({width,height:1000});await page.getByRole('button',{name:'预览 portrait.mp4',exact:true}).first().click();await page.locator('#media-modal').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#media-modal video')?.readyState>=1);await page.locator('#media-modal video').evaluate(v=>v.play());await page.waitForFunction(()=>document.querySelector('#media-modal video').currentTime>0);await page.screenshot({path:path.join(out,'review-preview-'+width+'.png')});await page.getByRole('button',{name:'关闭预览',exact:true}).click();}
  const result={outcome:'passed_pending_visual_review',business_state:run.state,cleanup:run.cleanup_pending,actual_harnesses:actual,media,
    driver_first_result:state.status,driver_first_error:state.error||null,scope:'Read-only actual artifacts, hash, full decode, dimensions, duration, audio presence and browser playback; visual frames reviewed separately'};
  save('review.json',result);console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
