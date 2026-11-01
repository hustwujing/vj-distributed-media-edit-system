const {chromium}=require('/data/wujing03/node_modules/playwright');
const {execFileSync}=require('node:child_process');
const fs=require('node:fs');
(async()=>{
 const [base,data,repo]=process.argv.slice(2);
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');
  const response=await page.request.post(base+'/api/projects',{data:{name:'Workflow browser smoke'}});
  if(response.status()!==201)throw Error(await response.text());
  const project=await response.json();
  execFileSync(repo+'/.venv/bin/python',['-m','tests.seed_browser_workflow',data,project.id],{cwd:repo});
  await page.goto(base+'/chat/'+project.id);
  await page.locator('.workflow-user-request').waitFor();
  await page.getByText('最终视频使用哪种画幅？',{exact:true}).waitFor();
  await page.locator('[data-answer]').selectOption({label:'横屏'});
  fs.mkdirSync(repo+'/docs/test-evidence',{recursive:true});
  await page.locator('.workflow-user-request').scrollIntoViewIfNeeded();
  await page.screenshot({path:repo+'/docs/test-evidence/user-decision.png',fullPage:true});
  await page.locator('[data-answer-action="answer"]').click();
  await page.locator('.workflow-user-request').waitFor({state:'detached'});
  const state=await (await page.request.get(base+'/api/projects/'+project.id+'/agent')).json();
  if(state.turns.at(-1).workflow.status!=='reviewing')throw Error('Decision did not queue diagnosis');
  if(errors.length)throw Error(errors.join('\n'));
  console.log(JSON.stringify({passed:true,decision:'selection',answer:'横屏',result:'reviewing',pageErrors:errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exit(1)});
