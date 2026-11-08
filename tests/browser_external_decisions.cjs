// Focused real Chromium check of the shipped decision renderer and click handler.
// Uses an isolated local page and mocked command API, never a deployed service.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../webapp/static');
const source=fs.readFileSync(path.join(root,'dynamic.js'),'utf8');
const command=source.slice(source.indexOf('async function command('),source.indexOf('\ndocument.querySelectorAll(\'[data-control]\')'));
const renderer=source.slice(source.indexOf('function syncAssetOptions('),source.indexOf("$('dynamic-input-form').onsubmit"));
assert.ok(command&&renderer);
const server=http.createServer((req,res)=>{
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');
    res.end('<!doctype html><base href="/"><meta name="viewport" content="width=device-width,initial-scale=1"><section id="dynamic-decisions"></section>');
    return;
  }
  const name=req.url.slice(1);
  if(!/^[a-z_]+\.js$/.test(name)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type','text/javascript');res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  const evidence=[];
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox']});
    for(const width of [1280,375]){
      const page=await browser.newPage({viewport:{width,height:900}});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      await page.goto('http://127.0.0.1:'+server.address().port+'/');
      await page.addScriptTag({type:'module',content:`
        import {$,esc,toast} from '/common.js';
        import {confirmationDetails} from '/dynamic_results.js';
        import {acceptsFeedback,decisionPresentation,assertCommandAccepted} from '/dynamic_state.js';
        let lastDecisions='',pendingCommand=null,current=null;
        const project={assets:[]},capabilities={operations:[]};
        const uuid=()=>crypto.randomUUID(),writeDraft=()=>{},readDraft=()=>null;
        const path=suffix=>'/api/projects/fixture'+suffix,refresh=async()=>{};
        window.requests=[];window.reply={id:'pending-command',status:'pending'};
        const api=async(url,options)=>{window.requests.push(JSON.parse(options.body));return window.reply;};
        ${command}
        ${renderer}
        window.mount=kind=>{
          lastDecisions='';pendingCommand=null;
          current={id:'run',tasks:[],dependencies:[],authorization:{},decisions:[{
            id:'decision',kind,snapshot_digest:'snapshot',request:{question:'请选择下一步',options:['keep_waiting','abandon_task']}}]};
          renderDecisions(current);
        };
        window.pending=()=>pendingCommand;
        window.ready=true;
      `});
      await page.waitForFunction(()=>window.ready);
      await page.evaluate(()=>window.mount('external_wait'));
      assert.equal(await page.getByRole('heading',{name:'外部任务等待超时'}).count(),1);
      assert.equal(await page.locator('[data-answer=approve]').count(),1);
      assert.equal(await page.locator('[data-answer=reject]').count(),1);
      assert.doesNotMatch(await page.locator('body').innerText(),/keep_waiting|abandon_task/);
      await page.getByRole('button',{name:'再等一个周期',exact:true}).click();
      assert.equal(await page.evaluate(()=>window.requests.at(-1).payload.action),'approve');
      await page.getByRole('button',{name:'放弃该任务并重新规划',exact:true}).click();
      assert.equal(await page.evaluate(()=>window.requests.at(-1).payload.action),'reject');
      await page.evaluate(()=>window.mount('external_submission'));
      assert.equal(await page.locator('[data-answer=approve]').count(),1);
      await page.locator('[data-external-id]').fill('original-job');
      await page.getByRole('button',{name:'提供 ID 并继续查询',exact:true}).click();
      assert.equal(await page.evaluate(()=>window.requests.at(-1).payload.external_id),'original-job');
      await page.evaluate(()=>{window.mount('external_wait');window.reply={status:'rejected',error_code:'external_recovery_cleanup_pending'};});
      await page.getByRole('button',{name:'再等一个周期',exact:true}).click();
      assert.match(await page.getByRole('alert').innerText(),/清理.*稍后/);
      assert.doesNotMatch(await page.getByRole('alert').innerText(),/请求已提交/);
      assert.equal(await page.evaluate(()=>window.pending()),null);
      assert.deepEqual(errors,[]);
      evidence.push({width,passed:true,checks:['named_wait_actions','single_approve','no_internal_enums','attach_id_payload','rejection_alert','no_success_on_rejection'],page_errors:errors});
      await page.close();
    }
    const report={passed:true,kind:'isolated_browser_with_mock_command_api',browser_version:browser.version(),evidence};
    if(process.argv[2]){
      const target=path.resolve(process.argv[2]);
      if(!target.startsWith(path.resolve(__dirname,'../..')+path.sep))throw Error('Evidence must remain under the workspace parent');
      fs.writeFileSync(target,JSON.stringify(report,null,2)+'\n');
    }
    console.log(JSON.stringify(report));
  }finally{
    if(browser)await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
