/* Six sequential live conversation scenarios; real browser/model/sandbox, no mocks. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..'),base='http://127.0.0.1/distributed-agent-sandbox';
const output=path.resolve(process.argv[2]||'');
assert.ok(output.startsWith('/data2/wujing03/')&&process.argv[2],'An explicit evidence directory on /data2/wujing03 is required');
const release=JSON.parse(fs.readFileSync(path.join(root,'.sandbox-cluster/release.json')));
assert.equal(release.namespace,'sandbox-agent');assert.equal(release.state,'ready');assert.ok(release.source.startsWith(root+'/.sandbox-cluster/releases/'));
fs.mkdirSync(output,{recursive:true,mode:0o700});
const state={total:6,started_at:Date.now()/1000,release,project:null,run_ids:[],cases:[],page_errors:[],status:'running'};
const save=(name,value)=>fs.writeFileSync(path.join(output,name),JSON.stringify(value,null,2)+'\n',{mode:0o600});
const persist=()=>save('state.json',state),uid=()=>crypto.randomBytes(16).toString('hex');
let activeCase;
const begin=(id,title)=>{activeCase={id,title,status:'running',started_at:Date.now()/1000};state.cases.push(activeCase);persist();console.log(JSON.stringify({event:'started',id,title}));};
const passed=()=>{activeCase.status='passed';activeCase.finished_at=Date.now()/1000;activeCase.seconds=activeCase.finished_at-activeCase.started_at;persist();console.log(JSON.stringify({event:'passed',...activeCase}));};
let browser,context,page,current,stopping=false;
const streams=new Map();
const api=async(method,suffix,data)=>{const response=await context.request.fetch(base+suffix,{method,...(data===undefined?{}:{data}),timeout:30000});assert.ok(response.ok(),`${method} ${suffix}: ${response.status()} ${await response.text()}`);return response.json();};
const read=async()=>{const run=await api('GET',`/api/projects/${state.project}/runs/${current}`);state.current={id:run.id,state:run.state,activity:run.activity,reason:run.terminal_reason,graph_version:run.graph_version};save('latest-run.json',run);persist();return run;};
const wait=async(predicate,seconds=600)=>{const until=Date.now()+seconds*1000;while(Date.now()<until&&!stopping){const run=await read();if(await predicate(run))return run;if(['FAILED','CANCELLED'].includes(run.state))throw Error('Run ended: '+run.state+' '+run.terminal_reason);await page.waitForTimeout(1000);}throw Error(stopping?'Interrupted':'Required boundary timed out');};
const send=async text=>{await page.locator('#dynamic-input-content').fill(text);const response=page.waitForResponse(r=>r.request().method()==='POST'&&/\/(runs|inputs)$/.test(new URL(r.url()).pathname));await page.locator('#dynamic-input-submit').click();const reply=await response;assert.equal(reply.status(),202,await reply.text());const body=await reply.json();current=body.run_id||body.id;if(!state.run_ids.includes(current))state.run_ids.push(current);persist();return current;};
const answer=async(decision,action)=>{const result=await api('POST',`/api/projects/${state.project}/runs/${current}/commands`,{request_id:uid(),action:'answer',payload:{decision_id:decision.id,snapshot_digest:decision.snapshot_digest,action}});return wait(run=>run.commands.some(c=>c.id===result.id&&c.status!=='pending'));};
const capture=async name=>{await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});};
const cancelOwned=async()=>{if(!context||!state.project)return;for(const id of state.run_ids){try{const run=await api('GET',`/api/projects/${state.project}/runs/${id}`);if(!['SUCCEEDED','FAILED','CANCELLED'].includes(run.state))await api('POST',`/api/projects/${state.project}/runs/${id}/commands`,{request_id:uid(),action:'cancel'});}catch(error){state.cleanup_error=error.message;}}};
process.on('SIGINT',()=>{stopping=true;});process.on('SIGTERM',()=>{stopping=true;});
(async()=>{
 try{
  execFileSync('ffmpeg',['-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2','-c:a','pcm_s16le',path.join(output,'input.wav')]);
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});context=await browser.newContext({viewport:{width:1440,height:1000}});page=await context.newPage();page.setDefaultTimeout(45000);
  page.on('pageerror',error=>{state.page_errors.push(error.message);persist();});page.on('request',request=>{if(request.url().endsWith('/stream'))streams.set(request.url(),(streams.get(request.url())||0)+1);});
  await context.request.get(base+'/');const project=await api('POST','/api/projects',{name:'Conversation recovery '+new Date().toISOString()});state.project=project.id;persist();
  const upload=await context.request.post(base+`/api/projects/${project.id}/assets`,{multipart:{file:{name:'input.wav',mimeType:'audio/wav',buffer:fs.readFileSync(path.join(output,'input.wav'))}}});assert.equal(upload.status(),201,await upload.text());
  await page.goto(base+'/chat/'+project.id);await context.storageState({path:path.join(output,'browser-state.json')});fs.chmodSync(path.join(output,'browser-state.json'),0o600);
  await page.locator('#dynamic-input-form').waitFor();await page.locator('#dynamic-execution-settings').evaluate(node=>node.open=true);
  await page.locator('#dynamic-model-profile').selectOption('claude55');await page.locator('#dynamic-reasoning-effort').selectOption('high');
  await page.locator('[data-operation]').evaluateAll(nodes=>nodes.forEach(node=>node.value='0'));
  await page.locator('[name=delivery-target]').evaluateAll(nodes=>nodes.forEach(node=>node.checked=false));await page.locator('#dynamic-targets-other').fill('');
  begin('LC01','先给方案，确认前不执行');
  await send('先给方案，确认前不要处理。把上传的音频降低 6 dB，保持时长、采样率和声道，使用 audio.process，输出一份 final.wav，不需要其他交付。');
  let run=await wait(run=>run.decisions.some(d=>d.kind==='plan_confirmation'));
  assert.equal(run.tasks.length,0);assert.equal(run.model_configuration.model,'claude-sonnet-5-5');
  let decision=run.decisions.find(d=>d.kind==='plan_confirmation');assert.ok(decision.request.delivery_targets.length);save('initial-plan.json',run);
  await page.waitForTimeout(4000);assert.equal((await read()).tasks.length,0);await capture('LC01-plan');passed();

  begin('LC02','修改待确认方案，旧确认失效');const old=decision;
  await send('方案改为只降低 3 dB，不是 6 dB；其他要求保持不变。仍请先给我确认，不要执行旧方案。');
  run=await wait(run=>run.decisions.some(d=>d.kind==='plan_confirmation'&&d.id!==old.id));
  decision=run.decisions.find(d=>d.kind==='plan_confirmation');assert.equal(run.tasks.length,0);
  assert.ok(decision.request.steps.some(step=>step.operation==='audio.process'&&step.parameters.gain_db===-3),JSON.stringify(decision.request.steps));
  const stale=await answer(old,'approve');assert.ok(stale.commands.some(c=>c.status==='rejected'));assert.equal(stale.tasks.length,0);save('revised-plan.json',run);await capture('LC02-revised-plan');passed();

  begin('LC03','确认后 OpenSandbox 执行、正式交付和终态稳定');
  const card=page.locator(`[data-decision="${decision.id}"]`);await card.locator('[data-answer=approve]').click();
  run=await wait(run=>run.state==='SUCCEEDED',900);assert.ok(run.delivery.length);assert.ok(run.tasks.some(task=>task.execution_location==='opensandbox'));
  assert.ok(run.external_cleanup.every(item=>item.state==='complete'));assert.equal(run.cleanup_pending,0);
  const final=run.delivery.find(item=>item.name==='final.wav');assert.ok(final,JSON.stringify(run.delivery));
  const download=await context.request.get(new URL(final.url,base+'/').href);assert.equal(download.status(),200);const bytes=await download.body();
  assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'),final.sha256);fs.writeFileSync(path.join(output,'final.wav'),bytes);
  const measured=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c',
   'import sys,wave,struct,math,json\ndef read(p):\n with wave.open(p) as w:\n  a=struct.unpack("<%dh"%(w.getnframes()*w.getnchannels()),w.readframes(w.getnframes()));return w.getframerate(),w.getnchannels(),len(a),math.sqrt(sum(x*x for x in a)/len(a))\na,b=map(read,sys.argv[1:]);print(json.dumps({"gain_db":20*math.log10(b[3]/a[3]),"same_format":a[:3]==b[:3]}))',path.join(output,'input.wav'),path.join(output,'final.wav')],{encoding:'utf8'}));
  assert.ok(measured.same_format);assert.ok(Math.abs(measured.gain_db+3)<.1);save('audio-independent-check.json',measured);
  await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();assert.ok(await page.locator('#dynamic-run-result a[download]').count());
  const before=streams.get(base+`/api/projects/${project.id}/runs/${current}/stream`);await page.waitForTimeout(5000);assert.equal(streams.get(base+`/api/projects/${project.id}/runs/${current}/stream`),before);
  save('completed-run.json',run);await capture('LC03-complete');passed();

  for(const [id,title,message] of [['LC04','追问成果在哪里','结果在哪里？请只告诉我上一轮完成了什么、如何查看，不要重新生成。'],['LC05','反馈页面异常时只解释','刚才页面不停闪烁滚动，让我不知道做了什么。请结合刚才的任务解释状态，不要处理音频，也不要再问我要剪辑还是配乐。']]){
   begin(id,title);await send(message);run=await wait(run=>run.state==='SUCCEEDED'||run.state==='WAITING_USER');
   assert.equal(run.terminal_reason,'conversation_answer',JSON.stringify(run.decisions));assert.equal(run.tasks.length,0);assert.equal(run.delivery.length,0);assert.ok(run.conversation_answer);assert.ok(run.parent_result?.delivery.some(item=>item.name==='final.wav'));
   await page.getByRole('heading',{name:'上一轮成果仍可使用'}).waitFor();save(id+'-run.json',run);await capture(id);passed();
  }
  begin('LC06','停止澄清方向，不自动重问');
  await send('下一步我想改变音频效果，但还没有决定具体怎么改。现在先问我需要哪种具体变化，不要默认选择，不要执行，也不要结束为已完成。');
  run=await wait(run=>run.decisions.some(d=>d.kind==='input_question')||run.state==='SUCCEEDED');assert.notEqual(run.state,'SUCCEEDED','Clarification boundary not reached');
  decision=run.decisions.find(d=>d.kind==='input_question');await answer(decision,'reject');run=await read();assert.equal(run.terminal_reason,'user_rejected');const plans=run.planners.length;
  await page.waitForTimeout(6000);run=await read();assert.equal(run.planners.length,plans);assert.equal(run.decisions.length,0);assert.equal(run.tasks.length,0);save('LC06-stopped.json',run);await capture('LC06');passed();
  assert.deepEqual(state.page_errors,[]);state.status='passed';
 }catch(error){state.status='failed';state.error=error.stack||error.message;if(activeCase&&activeCase.status==='running'){activeCase.status='failed';activeCase.finished_at=Date.now()/1000;activeCase.seconds=activeCase.finished_at-activeCase.started_at;}console.error(state.error);process.exitCode=1;}
 finally{await cancelOwned();state.finished_at=Date.now()/1000;persist();if(browser)await browser.close();}
})();
