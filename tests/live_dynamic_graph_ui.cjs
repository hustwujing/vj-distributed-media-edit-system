// Read-only public-page verification against existing owned campaign projects.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const [outArg,...caseArgs]=process.argv.slice(2),repo=path.resolve(__dirname,'..');
assert.ok(outArg&&caseArgs.length);const out=path.resolve(outArg),campaign=path.join(repo,'.sandbox-cluster/campaigns/demo-library');
assert.ok(out.startsWith(campaign+'/'));fs.mkdirSync(out,{recursive:true,mode:0o700});
const release=JSON.parse(fs.readFileSync(path.join(repo,'.sandbox-cluster/release.json')));
assert.equal(release.namespace,'sandbox-agent');assert.equal(release.state,'ready');
const base='http://updream-lab.bilibili.co/distributed-agent-sandbox-backend';
const save=(name,value)=>fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2)+'\n',{mode:0o600});
const primary={agent_worker:'宿主',executor_host:'宿主',control_host:'控制面',bwrap_sandbox:'bwrap',opensandbox:'OpenSandbox',unallocated:'待分配',unrecorded:'未记录',opensandbox_unconfirmed:'实例待确认'};
function expectedBadge(item){
 if(item.status==='waiting'&&['waiting','running'].includes(item.provider_execution_state))return '外部服务';
 let label=primary[item.execution_location]||'未记录';
 if(['bwrap_sandbox','opensandbox'].includes(item.tool_location)&&item.tool_location!==item.execution_location)label+=' · '+primary[item.tool_location];
 return label;
}
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP updream-lab.bilibili.co 127.0.0.1']}),results=[];
 try{for(const arg of caseArgs){
  const dir=path.resolve(arg);assert.ok(dir.startsWith(campaign+'/'));const state=JSON.parse(fs.readFileSync(path.join(dir,'state.json')));
  const context=await browser.newContext({storageState:path.join(dir,'browser-state.json'),viewport:{width:1440,height:1000}});
  const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  try{
   const cookie=(await context.cookies(base+'/')).map(c=>c.name+'='+c.value).join('; ');
   const response=await context.request.get('http://127.0.0.1/distributed-agent-sandbox/api/projects/'+state.project+'/runs/'+state.run_id,{headers:{Cookie:cookie}});assert.ok(response.ok());const run=await response.json();
   for(const task of run.tasks)assert.equal(typeof task.selected,'boolean');
   const items=new Map([...run.tasks.filter(t=>t.selected),...run.control_jobs].map(t=>[t.id,t]));if(run.control_jobs.length)items.set('__run__',null);
   await page.goto(base+'/chat/'+state.project);await page.locator('#dag-fab').waitFor({state:'visible'});
   assert.equal(await page.locator('#dynamic-activity-bundle').evaluate(n=>n.open),false);
   const record={case:state.case,project:state.project,run_id:state.run_id,release_sha:release.sha256,checks:[],viewports:[],locations:[...items].filter(([,item])=>item).map(([id,item])=>({id,type:item.type,revision:item.revision,control_kind:item.control_kind,execution_location:item.execution_location,tool_location:item.tool_location,execution_attempt_id:item.execution_attempt_id,provider_execution_state:item.provider_execution_state}))};
   for(const width of [1440,390]){
    await page.setViewportSize({width,height:1000});
    await page.locator('#dag-fab').click();await page.locator('#dag-dialog').waitFor({state:'visible'});
    await page.locator('#dag-reset').click();
    await page.waitForFunction(()=>{const v=document.querySelector('#dag-viewport'),c=document.querySelector('#dag-canvas');return c.offsetWidth<=v.clientWidth+1;});
    const layout=await page.locator('#dag-canvas').evaluate(canvas=>({width:canvas.offsetWidth,viewport:canvas.parentElement.clientWidth,
     nodes:[...canvas.querySelectorAll('[data-dag-node]')].map(n=>({id:n.dataset.dagNode,x:n.offsetLeft,y:n.offsetTop,w:n.offsetWidth,h:n.offsetHeight,badge:n.querySelector('.dag-location')?.textContent,title:n.querySelector('.dag-location')?.title})),
     edges:[...canvas.querySelectorAll('[data-edge-upstream]')].map(e=>({from:e.dataset.edgeUpstream,to:e.dataset.edgeDownstream,d:e.getAttribute('d')}))}));
    assert.equal(layout.nodes.length,items.size);assert.ok(layout.width<=layout.viewport+1);
    for(const n of layout.nodes){assert.ok(items.has(n.id));assert.equal(n.badge,n.id==='__run__'?'控制面':expectedBadge(items.get(n.id)));assert.ok(n.title);assert.ok(n.x>=0&&n.x+n.w<=layout.viewport+1);
     for(const b of layout.nodes)if(n.id!==b.id)assert.ok(n.x+n.w<=b.x||b.x+b.w<=n.x||n.y+n.h<=b.y||b.y+b.h<=n.y,`${n.id} overlaps ${b.id}`);
    }
    assert.ok(layout.edges.every(e=>e.d&&!e.d.includes('NaN')&&items.has(e.from)&&items.has(e.to)));
    record.viewports.push({width,layout});await page.screenshot({path:path.join(out,state.case+'-'+width+'.png'),fullPage:true});
    if(width===1440&&layout.nodes.length>1){
     const first=page.locator('[data-dag-node]').first(),id=await first.getAttribute('data-dag-node');await first.scrollIntoViewIfNeeded();const box=await first.boundingBox(),before=await first.evaluate(n=>[n.offsetLeft,n.offsetTop]);
     await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+25,box.y+box.height/2+30,{steps:5});await page.mouse.up();
     const moved=await first.evaluate(n=>[n.offsetLeft,n.offsetTop]);assert.notDeepEqual(moved,before);
     await page.reload();await page.locator('#dag-fab').waitFor({state:'visible'});await page.locator('#dag-fab').click();
     assert.deepEqual(await page.locator(`[data-dag-node="${id}"]`).evaluate(n=>[n.offsetLeft,n.offsetTop]),moved);
     await page.locator('#dag-reset').click();assert.deepEqual(await page.locator(`[data-dag-node="${id}"]`).evaluate(n=>[n.offsetLeft,n.offsetTop]),before);
     record.checks.push('dragged_node_saved_across_reload_and_auto_layout_restored');
    }
    await page.locator('#dag-close').click();
   }
   assert.deepEqual(errors,[]);record.checks.push('all_nodes_have_recorded_location_badges','retired_tasks_excluded','desktop_and_narrow_no_horizontal_overflow_or_overlapping_nodes','execution_process_defaults_closed');record.passed=true;record.page_errors=errors;results.push(record);save(state.case+'.json',record);console.log(JSON.stringify({case:state.case,passed:true,nodes:items.size}));
  }catch(error){const record={case:state.case,passed:false,error:error.message,page_errors:errors};results.push(record);save(state.case+'.json',record);await page.screenshot({path:path.join(out,state.case+'-failed.png'),fullPage:true});console.log(JSON.stringify(record));}
  finally{await context.close();}
 }}finally{await browser.close();save('summary.json',{release,total:results.length,passed:results.filter(x=>x.passed).length,results});}
 if(results.some(x=>!x.passed))process.exitCode=1;
})().catch(error=>{console.error(error.message);process.exitCode=1});
