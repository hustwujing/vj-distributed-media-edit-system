/* Read-only browser audit of completed success/failure transcripts in an owned QA project. */
const {chromium}=require('/data/wujing03/node_modules/playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const out=path.resolve(process.argv[2]),state=JSON.parse(fs.readFileSync(path.join(out,'report.json'))),base=process.env.VU_CASE_BASE||'http://10.194.49.8/video-use';
(async()=>{const b=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 const c=await b.newContext({storageState:path.join(out,'browser-state.json'),viewport:{width:1440,height:1100}}),p=await c.newPage(),errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.goto(base+'/chat/'+state.project);const r=await c.request.get(base+`/api/projects/${state.project}/agent`);assert.ok(r.ok());const turns=(await r.json()).turns,checks=[];
 for(const t of turns){assert.ok(['succeeded','failed','cancelled','paused'].includes(t.status));await p.waitForFunction(({id,n})=>document.querySelector(`[data-turn="${id}"]`)?.querySelectorAll('.public-message').length===n,{id:t.id,n:t.messages.length});
  const v=await p.evaluate(async t=>{const {markdown}=await import('/video-use/static/common.js'),node=document.querySelector(`[data-turn="${t.id}"]`),parts=[...node.querySelectorAll('.public-message')],normalize=s=>s.replace(/\s+/g,'');return {id:t.id,status:t.status,messages:t.messages.length,all_messages_match:t.messages.every((m,i)=>{const expected=document.createElement('div');expected.innerHTML=markdown(m.text,t.artifacts);const actual=parts[i].cloneNode(true);actual.querySelector('small')?.remove();return normalize(expected.textContent)===normalize(actual.textContent);}),error_matches:normalize(node.querySelector('.turn-error').textContent)===normalize(t.error||''),tool_spinners:node.querySelectorAll('.tool-activity .spinner').length};},t);
  assert.ok(v.all_messages_match);assert.ok(v.error_matches);assert.equal(v.tool_spinners,0);checks.push(v);
 }
 await p.screenshot({path:path.join(out,'terminal-audit.png'),fullPage:true});assert.deepEqual(errors,[]);const report={passed:true,checks,page_errors:errors};fs.writeFileSync(path.join(out,'terminal-audit.json'),JSON.stringify(report,null,2),{mode:0o600});console.log(JSON.stringify(report));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
