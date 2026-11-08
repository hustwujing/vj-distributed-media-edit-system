// Collect authorized immutable task sidecars for independent artifact review.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {request}=require('playwright');
const dir=path.resolve(process.argv[2]||'');
if(!dir.startsWith('/data2/wujing03/')||!process.argv[2])throw Error('Explicit isolated case evidence directory required');
(async()=>{
 const state=JSON.parse(fs.readFileSync(path.join(dir,'state.json'))),run=JSON.parse(fs.readFileSync(path.join(dir,'final-run.json')));
 const ctx=await request.newContext({storageState:path.join(dir,'browser-state.json')});
 try{
  const storage=await ctx.storageState(),cookie=storage.cookies.filter(c=>c.domain==='updream-lab.bilibili.co').map(c=>c.name+'='+c.value).join('; ');
  const out=path.join(dir,'review-evidence');fs.mkdirSync(out,{recursive:true,mode:0o700});const records=[];
  for(const task of run.tasks){
   for(const ref of task.accepted_result.filter(ref=>/\.(json|srt|ass|txt|md)$/.test(ref))){
    const response=await ctx.get(`http://127.0.0.1/distributed-agent-sandbox/api/projects/${state.project}/runs/${state.run_id}/tasks/${task.id}/file`,{params:{file:ref},headers:{Cookie:cookie},timeout:60000});
    if(!response.ok())throw Error(`${task.id}: ${response.status()} fetching declared sidecar`);
    const bytes=await response.body(),name=task.id+'-'+crypto.createHash('sha256').update(ref).digest('hex').slice(0,12)+'-'+path.basename(ref);
    fs.writeFileSync(path.join(out,name),bytes,{mode:0o600});records.push({task:task.id,ref,file:name,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')});
   }
  }
  fs.writeFileSync(path.join(out,'index.json'),JSON.stringify({run_id:run.id,release_sha:state.release_sha,records},null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({case:state.case,sidecars:records.length}));
 }finally{await ctx.dispose();}
})().catch(error=>{console.error(error);process.exitCode=1;});
