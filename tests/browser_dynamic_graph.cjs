// Actual graph renderer and styles with isolated data; no application writes.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../webapp/static'),source=fs.readFileSync(path.join(root,'dynamic.js'),'utf8');
const section=(start,end)=>{const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a);return source.slice(a,b);};
const renderer=section('const activeNodeStatuses=', 'function conversationAtBottom('),resize=section('const dagResizeObserver=', 'preview=new Preview(');
const server=http.createServer((req,res)=>{
 if(req.url==='/'){
  res.setHeader('Content-Type','text/html');res.end('<!doctype html><base href="/"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/studio.css"><link rel="stylesheet" href="/dynamic.css"><button id="dag-fab"><span id="dag-fab-count"></span></button><dialog id="dag-dialog" class="dag-dialog"><header><h2>运行图</h2><button id="dag-reset">恢复自动布局</button></header><div class="dag-body"><div id="dag-viewport" class="dag-viewport"><div id="dag-canvas" class="dag-canvas"></div></div><aside id="dag-detail" class="dag-detail"></aside></div></dialog>');return;
 }
 const name=req.url.slice(1);if(!/^[a-z_]+\.(js|css)$/.test(name)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':'text/css');res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;const records=[];
 try{
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port+'/');
  await page.addScriptTag({type:'module',content:`
   import {$,esc,href} from '/common.js';
   import {layoutDag,dagEdgePath} from '/dynamic_dag.js';
   import {executionLocation,executionPlace} from '/dynamic_locations.js';
   import {configurationText} from '/dynamic_models.js';
   const projectId='isolated-graph',project={assets:[]},path=suffix=>'/unused'+suffix,names={succeeded:'已完成',running:'执行中'};
   const safeName=item=>item.spec.name,activityRole=item=>item.control_kind||'代码编辑师',activityLocation=item=>executionLocation(item.execution_location).label,activityAction=item=>item.status;
   let dagGraph=null,dagLayoutId='',lastDagSignature='',lockedDagNode=null,dagViewportWidth=760;
   const current={id:'run',graph_version:1,state:'ACTIVE',tasks:Array.from({length:18},(_,i)=>({id:'task-'+i,type:'operation',selected:true,spec:{name:'任务 '+i,objective:'验证布局',inputs:{},outputs:[]},status:'succeeded',revision:0,revision_graph_version:1,attempts:1,updated_at:1,accepted_result:[],candidate_result:[],execution_location:i%3===0?'opensandbox':i%3===1?'bwrap_sandbox':'executor_host'})),dependencies:Array.from({length:17},(_,i)=>({upstream_task_id:'task-'+i,downstream_task_id:'task-'+(i+1)})),control_jobs:[],planners:[],reviews:[]};
   ${renderer}
   ${resize}
   $('dag-reset').onclick=()=>{sessionStorage.removeItem(dagStorageKey(current));lastDagSignature='';renderDag(current);};
   $('dag-dialog').showModal();renderDag(current);
   window.graph=()=>({nodes:dagGraph.nodes.map(n=>({id:n.id,band:n.band,direction:n.direction,x:n.x,y:n.y})),width:dagGraph.width});
   window.updateLocation=()=>{current.tasks[0].execution_location='agent_worker';current.tasks[0].tool_location='bwrap_sandbox';renderDag(current);};
   window.rerender=()=>renderDag(current);window.ready=true;
  `});
  await page.waitForFunction(()=>window.ready);
  for(const width of [1440,390,1024]){
   await page.setViewportSize({width,height:1000});
   await page.waitForFunction(()=>{const v=document.querySelector('#dag-viewport'),c=document.querySelector('#dag-canvas');return Math.abs(c.offsetWidth-v.clientWidth)<=1&&Math.abs(window.graph().width-v.clientWidth)<=1;});
   const layout=await page.evaluate(()=>({graph:window.graph(),viewport:document.querySelector('#dag-viewport').clientWidth,nodes:[...document.querySelectorAll('[data-dag-node]')].map(n=>({x:n.offsetLeft,y:n.offsetTop,w:n.offsetWidth,h:n.offsetHeight,badge:n.querySelector('.dag-location').textContent}))}));
   assert.equal(layout.nodes.length,18);assert.ok(new Set(layout.graph.nodes.map(n=>n.band)).size>=3);
   for(let i=0;i<layout.nodes.length;i++){const a=layout.nodes[i];assert.ok(a.x>=0&&a.x+a.w<=layout.viewport+1);assert.ok(a.badge);for(const b of layout.nodes.slice(i+1))assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y);}
   records.push({width,passed:true,bands:new Set(layout.graph.nodes.map(n=>n.band)).size,viewport:layout.viewport});
  }
  await page.evaluate(()=>window.updateLocation());assert.equal(await page.locator('[data-dag-node="task-0"] .dag-location').innerText(),'宿主 · bwrap','Location-only change must invalidate the render cache');
  const node=page.locator('[data-dag-node="task-0"]');await node.scrollIntoViewIfNeeded();const box=await node.boundingBox(),before=await node.evaluate(n=>[n.offsetLeft,n.offsetTop]);
  const edges=await page.locator('[data-edge-upstream]').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('d')));
  await page.mouse.move(box.x+50,box.y+30);await page.mouse.down();await page.mouse.move(box.x+70,box.y+60,{steps:5});await page.mouse.up();
  const moved=await node.evaluate(n=>[n.offsetLeft,n.offsetTop]);assert.notDeepEqual(before,moved);
  assert.notDeepEqual(await page.locator('[data-edge-upstream]').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('d'))),edges);
  await page.evaluate(()=>window.rerender());assert.deepEqual(await node.evaluate(n=>[n.offsetLeft,n.offsetTop]),moved);
  await page.locator('#dag-reset').click();assert.deepEqual(await node.evaluate(n=>[n.offsetLeft,n.offsetTop]),before);
  assert.deepEqual(errors,[]);const report={passed:true,kind:'isolated_browser_actual_graph_renderer',records,location_cache_update:true,drag_edges_and_restore:true,page_errors:errors};
  if(process.argv[2])fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
