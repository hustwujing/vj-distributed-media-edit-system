const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const base=process.argv[2]||'http://127.0.0.1/distributed-agent-system';
const out=process.argv[3]||path.resolve('.cluster/ui-review/dynamic-workspace');
const now=Date.now()/1000;
const task=(id,name,status,type,objective)=>({id,type,spec:{name,objective,inputs:{source:{asset:'素材 A'}},output_roles:['primary_media'],semantics:{consistency_mode:'preserve'}},status,selected:true,phase:status==='succeeded'?'completed':'executing',revision:1,authorization_version:1,repair_count:0,user_revision_count:0,attempts:status==='running'?1:0,wait_reason:status==='blocked'?'等待上游步骤':null,candidate_result:[],accepted_result:[],quality:{},error_code:null,error_detail:null,created_at:now-60,updated_at:now});
(async()=>{
 fs.mkdirSync(out,{recursive:true});const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();const errors=[];page.on('pageerror',error=>errors.push(error.message));let project;
 try{
  const created=await context.request.post(base+'/api/projects',{data:{name:'Dynamic workspace browser check'}});assert.equal(created.status(),201);project=(await created.json()).id;
  const run={id:'ui-run',project_id:project,session_id:'ui-session',parent_run_id:null,scope_confirmed:true,goal:'把素材剪成节奏清晰的短片，并完成视觉验收',state:'EXECUTING',activity:'running',graph_version:3,sealed:false,authorization:{model_token_budget:200000},authorization_version:1,replan_after:null,repair_context:{},created_at:now-90,updated_at:now,terminal_reason:null,delivery:[],delivered_at:null,input_sequence:0,input_barrier_sequence:null,successor_run_id:null,model_configuration:{harness:'claude_code',model:'claude-sonnet-5-5',reasoning_effort:'high'},tasks:[task('inspect','分析素材','succeeded','agent','识别镜头结构和可用片段'),task('edit','完成主剪辑','running','operation','按分析结果完成主剪辑'),task('review','视觉验收','blocked','agent','检查画面、节奏与交付要求')],control_jobs:[{id:'manager',control_kind:'PLAN',status:'completed',phase:'completed',attempts:1,error_code:null,created_at:now-90,updated_at:now-70},{id:'quality',control_kind:'REVIEW',status:'running',phase:'executing',attempts:1,error_code:null,created_at:now-20,updated_at:now}],inputs:[],drain_set:[],dependencies:[{upstream_task_id:'inspect',downstream_task_id:'edit'},{upstream_task_id:'edit',downstream_task_id:'review'}],replacements:[],planners:[],plan_drafts:[],decisions:[],reviews:[],commands:[],diagnoses:[],external_cleanup:[],token_usage:{known:1200,reserved_or_unknown:0,remaining:198800,limit:200000},cleanup_pending:0};
  await page.route(`**/api/projects/${project}/runs`,route=>route.request().method()==='GET'?route.fulfill({json:{runs:[{id:run.id,goal:run.goal,state:run.state,graph_version:run.graph_version}]}}):route.continue());
  await page.route(`**/api/projects/${project}/runs/ui-run`,route=>route.fulfill({json:run}));
  await page.route(`**/api/projects/${project}/runs/ui-run/events?*`,route=>route.fulfill({json:{events:[]}}));
  const response=await page.goto(`${base}/chat/${project}`,{waitUntil:'networkidle'});assert.equal(response.status(),200);
  await page.waitForFunction(()=>document.querySelector('#dynamic-connection')?.textContent.includes('同步'));
  assert.ok(await page.locator('#dynamic-preview').isVisible());assert.ok(await page.locator('#dag-fab').isVisible());
  assert.equal(await page.locator('#dynamic-tasks').isVisible(),false);assert.equal(await page.locator('#dynamic-events').isVisible(),false);
  assert.notEqual(await page.locator('#dag-fab').evaluate(node=>getComputedStyle(node).animationName),'none');
  await page.screenshot({path:path.join(out,'desktop-workspace.png'),fullPage:true});
  await page.locator('#dag-fab').click();await page.locator('#dag-dialog').waitFor({state:'visible'});
  assert.equal(await page.locator('.dag-node.task').count(),3);assert.equal(await page.locator('.dag-node.control').count(),2);assert.equal(await page.locator('.dag-edge').count(),2);
  const active=page.locator('.dag-node.active').first();assert.notEqual(await active.evaluate(node=>getComputedStyle(node).animationName),'none');await active.hover();assert.ok((await page.locator('#dag-detail').innerText()).includes('尝试'));
  await page.screenshot({path:path.join(out,'desktop-dag.png'),fullPage:true});await page.locator('#dag-close').click();
  await page.setViewportSize({width:390,height:844});assert.ok(await page.locator('#dynamic-input-form').isVisible());assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  await page.screenshot({path:path.join(out,'mobile-workspace.png'),fullPage:true});await page.locator('#dag-fab').click();await page.locator('#dag-dialog').waitFor({state:'visible'});await page.screenshot({path:path.join(out,'mobile-dag.png')});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,project,screenshots:fs.readdirSync(out)}));
 }finally{if(project)await context.request.delete(`${base}/api/projects/${project}`);await browser.close();}
})().catch(error=>{console.error(error.stack||error.message);process.exitCode=1;});
