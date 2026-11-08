// Actual application renderers/styles, isolated fixture data, no service writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../webapp/static'),source=fs.readFileSync(path.join(root,'dynamic.js'),'utf8');
const section=(start,end)=>{const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a);return source.slice(a,b);};
const activity=section('function renderChatActivity(', 'function renderBlockingAction(');
const detail=section('function detailValue(', 'function dagStorageKey(');
const sample='第一段：核对素材并制定方案。\n\n第二段：执行处理并检查交付。\n\n- 检查音轨\n- 检查时长\n\n```python\nprint(1)\nprint(2)\n```';
const server=http.createServer((req,res)=>{
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');
    res.end(`<!doctype html><base href="/"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="stylesheet" href="/studio.css"><link rel="stylesheet" href="/dynamic.css">
      <main class="dynamic-conversation"><article class="assistant-message transcript">
      <section id="dynamic-role-activity"><span id="dynamic-role-count"></span><div id="dynamic-role-items"></div></section>
      <details id="dynamic-activity-bundle" class="activity-bundle" open><summary><span id="dynamic-activity-summary"></span><span id="dynamic-activity-time"></span></summary>
      <div class="activity-body"><div id="dynamic-activity-plan"></div><div id="dynamic-activity-tools"></div></div></details>
      <div class="agent-output-list"><section class="agent-output-entry"><div class="agent-output-meta"><span>任务规划师</span></div><div class="assistant-text markdown" id="output"></div></section></div>
      </article></main><aside id="dag-detail"></aside>`);return;
  }
  const name=req.url.slice(1);
  if(!/^[a-z_]+\.(js|css)$/.test(name)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',name.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;const evidence=[];
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox']});
    for(const width of [1280,375]){
      const page=await browser.newPage({viewport:{width,height:900}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto('http://127.0.0.1:'+server.address().port+'/');
      await page.addScriptTag({type:'module',content:`
        import {$,esc,markdown,href} from '/common.js';
        import {terminalRun} from '/dynamic_state.js';
        const names={running:'执行中'},liveEvents=[],safeName=item=>item.spec.name;
        ${activity}\n${detail}
        const now=Date.now()/1000,item={id:'mix',type:'operation',spec:{name:'混音',operation:'media.edit'},status:'running',revision:0,attempts:1,created_at:now,updated_at:now};
        const run={id:'run',state:'ACTIVE',created_at:now,tasks:[item],control_jobs:[]};
        window.mount=location=>{item.execution_location=location;renderRoleActivity(run);renderChatActivity(run);renderDagDetail({kind:'task',item});};
        window.expert=()=>renderDagDetail({kind:'control',item:{...item,type:'agent',control_kind:'REVIEW',execution_location:'agent_worker',tool_location:'bwrap_sandbox'}});
        $('output').innerHTML=markdown(${JSON.stringify(sample)},[]);
        window.ready=true;
      `});
      await page.waitForFunction(()=>window.ready);
      for(const [location,label] of [['unallocated','待分配'],['opensandbox_unconfirmed','实例未确认'],['opensandbox','OpenSandbox 沙盒'],['bwrap_sandbox','bwrap 沙盒'],['unrecorded','执行位置未记录'],[null,'执行位置未记录']]){
        await page.evaluate(value=>window.mount(value),location);
        assert.ok((await page.locator('#dynamic-role-items').innerText()).includes(label));
        assert.ok((await page.locator('#dag-detail').innerText()).includes(label));
      }
      await page.evaluate(()=>window.expert());
      const expert=await page.locator('#dag-detail').innerText();
      assert.match(expert,/Agent Worker 宿主/);assert.match(expert,/工具位置\nbwrap 沙盒/);
      const layout=await page.evaluate(()=>{
        const text=getComputedStyle(document.querySelector('#output')),p=getComputedStyle(document.querySelector('#output p'));
        const step=getComputedStyle(document.querySelector('.plan-step'));
        return {line_ratio:parseFloat(text.lineHeight)/parseFloat(text.fontSize),paragraph_gap:parseFloat(p.marginBottom),
          whitespace:text.whiteSpace,step_padding:parseFloat(step.paddingTop),code:document.querySelector('#output pre').innerText,
          overflow:document.documentElement.scrollWidth>innerWidth};
      });
      assert.ok(layout.line_ratio<=1.56);assert.equal(layout.paragraph_gap,6);assert.equal(layout.whitespace,'normal');
      assert.equal(layout.step_padding,2);assert.match(layout.code,/print\(1\)\nprint\(2\)/);assert.equal(layout.overflow,false);
      assert.deepEqual(errors,[]);
      evidence.push({width,passed:true,layout,page_errors:errors});await page.close();
    }
    const report={passed:true,kind:'isolated_browser_actual_renderers_fixture_data',browser:browser.version(),evidence};
    if(process.argv[2])fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report));
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
