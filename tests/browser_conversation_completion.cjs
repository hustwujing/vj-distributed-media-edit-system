// Full shipped page with isolated HTTP/SSE fixtures. No model or service writes.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../webapp/static'),project='a'.repeat(32),id='b'.repeat(32),followup='c'.repeat(32),now=Date.now()/1000;
const asset={id:'source',name:'input.png',kind:'image',url:'source.png',width:1,height:1};
const file={name:'final.png',file:'final.png',type:'image/png',url:'final.png',is_primary:true,workflow_delivery:true};
const task={id:'edit',type:'operation',spec:{name:'处理素材',objective:'生成已确认的结果',operation:'image.local_adjust',parameters:{brightness:1.1}},status:'running',selected:true,phase:'local',revision:0,revision_graph_version:1,attempts:1,accepted_result:[],candidate_result:[],quality:{},created_at:now,updated_at:now};
const run={id,project_id:project,state:'WAITING_USER',activity:'PLANNING',scope_confirmed:true,goal:'先给方案，确认后生成结果',graph_version:0,authorization_version:1,authorization:{delivery_targets:[],allowed_assets:[],allowed_operations:[],allowed_agent_profiles:[],provider_call_budget:{},content_fingerprint:'a'.repeat(64)},
  created_at:now,updated_at:now,input_sequence:0,input_barrier_sequence:null,tasks:[],control_jobs:[],inputs:[],dependencies:[],replacements:[],planners:[],plan_drafts:[],commands:[],diagnoses:[],reviews:[],external_cleanup:[],delivery:[],parent_result:null,token_usage:{limit:10000,remaining:10000},
  decisions:[{id:'plan',kind:'plan_confirmation',snapshot_digest:'d'.repeat(64),request:{reason:'先调整亮度，再输出图片',question:'确认后执行',delivery_targets:['final.png'],steps:[task.spec]}}]};
const other={...run,id:followup,state:'SUCCEEDED',activity:'IDLE',goal:'结果在哪？',terminal_reason:'conversation_answer',conversation_answer:'上一轮已经完成，final.png 可以直接预览；本次没有重新生成。',decisions:[],parent_result:{id,goal:run.goal,delivery:[file]}};
const streams=new Map(),counts=new Map(),requests=[],errors=[];
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
 if(req.method!=='GET'){
  let raw='';for await(const chunk of req)raw+=chunk;const command=JSON.parse(raw);requests.push(command);
  assert.equal(command.action,'answer');assert.equal(command.payload.action,'approve');
  run.state='ACTIVE';run.graph_version=1;run.authorization_version=2;run.authorization.delivery_targets=['final.png'];run.decisions=[];run.tasks=[task];run.updated_at++;
  run.commands=[{id:command.request_id,status:'applied'}];json(run.commands[0]);return;
 }
 if(url.pathname.startsWith('/chat/')){res.setHeader('Content-Type','text/html');res.end(fs.readFileSync(path.join(root,'dynamic.html'),'utf8').replace('{{BASE}}','/').replace('{{PROJECT}}',project));return;}
 if(url.pathname==='/source.png'||url.pathname==='/final.png'){res.setHeader('Content-Type','image/png');res.end(png);return;}
 if(url.pathname.startsWith('/static/')){const name=url.pathname.slice(8);if(!/^[a-z_]+\.(css|js)$/.test(name)){res.writeHead(404).end();return;}res.setHeader('Content-Type',name.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(path.join(root,name)));return;}
 const prefix='/api/projects/'+project;
 if(url.pathname===prefix){json({id:project,name:'对话回归',assets:[asset],draft:{},jobs:[]});return;}
 if(url.pathname===prefix+'/runs/capabilities'){json({operations:[],profiles:[],model_choices:{profiles:[]}});return;}
 if(url.pathname===prefix+'/runs'){json({runs:[run,other]});return;}
 const match=url.pathname.match(/\/runs\/([^/]+)(.*)/);if(!match){res.writeHead(404).end();return;}
 const current=match[1]===id?run:other;
 if(match[2]===''){json(current);return;}
 if(match[2]==='/events'){json({events:[]});return;}
 if(match[2]==='/stream'){
  counts.set(current.id,(counts.get(current.id)||0)+1);res.setHeader('Content-Type','text/event-stream');res.setHeader('Cache-Control','no-cache');res.flushHeaders();
  const text='任务规划师：'+Array.from({length:50},(_,i)=>'公开过程说明 '+i).join('\n');
  res.write('event: text_snapshot\ndata: '+JSON.stringify({text,text_version:[1,1],text_phase:'idle',published_at:now-30})+'\n\n');
  if(['SUCCEEDED','FAILED','CANCELLED'].includes(current.state)){res.end('event: done\ndata: {}\n\n');return;}
  streams.set(current.id,res);req.on('close',()=>{if(streams.get(current.id)===res)streams.delete(current.id);});return;
 }
 res.writeHead(404).end();
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;const evidence=[];
 try{
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1280,height:900}});page.on('pageerror',error=>errors.push(error.message));
  await page.goto('http://127.0.0.1:'+server.address().port+'/chat/'+project);
  await page.getByRole('button',{name:'确认方案并执行',exact:true}).waitFor();
  assert.match(await page.locator('.plan-confirmation-details').innerText(),/final.png/);
  assert.equal(run.tasks.length,0);assert.equal(requests.length,0);
  await page.getByRole('button',{name:'确认方案并执行',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#dynamic-current').dataset.graphVersion==='1');
  assert.equal(await page.locator('#dynamic-targets-other').inputValue(),'final.png');
  run.state='SUCCEEDED';run.activity='IDLE';run.sealed=true;run.delivery=[file];run.updated_at++;task.status='succeeded';
  streams.get(id).end('event: done\ndata: {}\n\n');streams.delete(id);
  await page.getByRole('heading',{name:'任务已完成',exact:true}).waitFor();
  assert.match(await page.locator('#dynamic-preview img').getAttribute('src'),/final.png$/);
  assert.equal(await page.locator('#dynamic-run-result a[download]').count(),1);
  const text=page.locator('[data-agent-output]').first();await text.evaluate(node=>node.dataset.stabilityProbe='same-node');
  await page.locator('#dynamic-conversation').evaluate(node=>{node.scrollTop=0;node.dispatchEvent(new Event('scroll'));});
  const before=await page.locator('#dynamic-conversation').evaluate(node=>node.scrollTop);
  await page.waitForTimeout(4500);
  assert.equal(counts.get(id),1);assert.equal(await text.getAttribute('data-stability-probe'),'same-node');
  assert.equal(await page.locator('#dynamic-conversation').evaluate(node=>node.scrollTop),before);
  evidence.push('one_terminal_stream_stable_dom_and_scroll','confirmed_targets_backfilled','first_delivery_replaces_source_preview');
  await page.locator('[data-run="'+followup+'"]').click();await page.locator('#dag-close').click();
  await page.getByRole('heading',{name:'上一轮成果仍可使用'}).waitFor();
  assert.match(await page.locator('#dynamic-run-result').innerText(),/没有重新生成/);
  await page.locator('#dynamic-run-result [data-result-preview]').click();assert.match(await page.locator('#dynamic-preview img').getAttribute('src'),/final.png$/);
  await page.locator('[data-run="'+id+'"]').click();await page.locator('#dag-close').click();
  await page.waitForTimeout(2500);assert.equal(counts.get(id),2);
  evidence.push('read_only_answer_keeps_prior_result','history_reentry_replays_once');
  await page.setViewportSize({width:375,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  const report={passed:true,scope:'full_application_page_with_fixture_http_and_sse',checks:evidence,page_errors:errors};
  if(process.argv[2]){fs.mkdirSync(process.argv[2],{recursive:true});await page.screenshot({path:path.join(process.argv[2],'mobile.png'),fullPage:true});fs.writeFileSync(path.join(process.argv[2],'report.json'),JSON.stringify(report,null,2)+'\n');}
  console.log(JSON.stringify(report));
 }finally{for(const stream of streams.values())stream.end();if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
