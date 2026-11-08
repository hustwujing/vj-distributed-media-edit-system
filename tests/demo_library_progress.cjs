// Read-only summary; business completion and independent review stay separate.
const fs=require('node:fs'),path=require('node:path');
const root=path.resolve(process.argv[2]||'.sandbox-cluster/campaigns/demo-library');
const latest=new Map();
for(const round of fs.readdirSync(root,{withFileTypes:true}).filter(x=>x.isDirectory())){
 for(const item of fs.readdirSync(path.join(root,round.name),{withFileTypes:true}).filter(x=>x.isDirectory())){
  const dir=path.join(root,round.name,item.name),file=path.join(dir,'state.json');if(!fs.existsSync(file))continue;
  const state=JSON.parse(fs.readFileSync(file));if(!state.run_id)continue;
  if(latest.has(state.case)&&latest.get(state.case).started_at>state.started_at)continue;
  const reviewFile=path.join(dir,'review.json');latest.set(state.case,{...state,round:round.name,
   review:fs.existsSync(reviewFile)?JSON.parse(fs.readFileSync(reviewFile)):null});
 }
}
const cases=[...latest.values()],count=predicate=>cases.filter(predicate).length;
const passed=x=>x.review?.outcome==='passed';
const failure=x=>x.review?.outcome||x.classification||'';
const finished=cases.filter(x=>Number.isFinite(x.seconds));
const businessComplete=finished.filter(x=>x.status==='passed_pending_review');
const average=xs=>xs.length?Math.round(xs.reduce((sum,x)=>sum+x.seconds,0)/xs.length):null;
console.log(JSON.stringify({total:44,started:cases.length,not_started:44-cases.length,passed:count(passed),
 program_failed:count(x=>!passed(x)&&['program_failed','contract_failed','prompt_failed'].includes(failure(x))),
 nonprogram_failed:count(x=>failure(x)==='nonprogram_failed'),
 pending_review:count(x=>!x.review&&x.status==='passed_pending_review'),
 pending_analysis:count(x=>!x.review&&x.status==='failed'&&!['program_failed','contract_failed','prompt_failed','nonprogram_failed'].includes(failure(x))),
 active:count(x=>!x.finished_at),average_finished_seconds:average(finished),average_business_completed_seconds:average(businessComplete),
 active_cases:cases.filter(x=>!x.finished_at).map(x=>({case:x.case,phase:x.phase,elapsed_seconds:Math.round(Date.now()/1000-x.started_at)}))},null,2));
