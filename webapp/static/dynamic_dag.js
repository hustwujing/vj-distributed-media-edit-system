const CONTROL_HEIGHT=54;
const TASK_WIDTH=174;
const TASK_HEIGHT=62;

export function layoutDag(tasks=[],dependencies=[],controlJobs=[],relations={}){
 const selected=tasks.filter(task=>task.selected!==false);
 const ids=new Set(selected.map(task=>task.id));
 const parents=new Map(selected.map(task=>[task.id,[]]));
 const children=new Map(selected.map(task=>[task.id,[]]));
 const edges=[],edgeKeys=new Set();
 const addEdge=(upstream,downstream,kind='business')=>{const key=`${kind}:${upstream}:${downstream}`;if(upstream!==downstream&&!edgeKeys.has(key)){edgeKeys.add(key);edges.push({upstream,downstream,kind});}};
 for(const edge of dependencies){
  const upstream=edge.upstream_task_id,downstream=edge.downstream_task_id;
  if(!ids.has(upstream)||!ids.has(downstream)||upstream===downstream)continue;
  parents.get(downstream).push(upstream);children.get(upstream).push(downstream);
  addEdge(upstream,downstream);
 }
 const levels=new Map(selected.map(task=>[task.id,0]));
 const indegree=new Map(selected.map(task=>[task.id,parents.get(task.id).length]));
 const queue=selected.filter(task=>indegree.get(task.id)===0).map(task=>task.id);
 for(let index=0;index<queue.length;index++){
  const id=queue[index];
  for(const child of children.get(id)){
   levels.set(child,Math.max(levels.get(child),levels.get(id)+1));
   indegree.set(child,indegree.get(child)-1);if(indegree.get(child)===0)queue.push(child);
  }
 }
 // A malformed historic cycle must remain inspectable even though new plans reject it.
 const unresolved=selected.filter(task=>indegree.get(task.id)>0);
 const fallback=Math.max(0,...levels.values())+1;
 unresolved.forEach((task,index)=>levels.set(task.id,fallback+index));
 const rows=new Map();
 const taskNodes=selected.map(task=>{
  const level=levels.get(task.id),row=rows.get(level)||0;rows.set(level,row+1);
  return {kind:'task',id:task.id,item:task,x:36+level*220,y:112+row*92,width:TASK_WIDTH,height:TASK_HEIGHT};
 });
 const runNode=controlJobs.length?{kind:'run',id:'__run__',item:relations.run||{},x:24,y:30,width:TASK_WIDTH,height:CONTROL_HEIGHT}:null;
 const controlNodes=controlJobs.map((job,index)=>({kind:'control',id:job.id,item:job,x:230+index*196,y:30,width:TASK_WIDTH,height:CONTROL_HEIGHT}));
 const controlIds=new Set(controlJobs.map(job=>job.id));
 const planners=new Map((relations.planners||[]).map(planner=>[planner.task_id,planner]));
 const reviews=new Map((relations.reviews||[]).map(review=>[review.task_id,review]));
 const leaves=selected.filter(task=>!children.get(task.id).length).map(task=>task.id);
 for(const job of controlJobs){
  if(job.control_kind==='PLAN'){
   const planner=planners.get(job.id),sources=[];
   for(const id of planner?.trigger_task_ids||[])if(ids.has(id))sources.push(id);
   for(const id of planner?.trigger_control_job_ids||[])if(controlIds.has(id))sources.push(id);
   if(sources.length)for(const id of sources)addEdge(id,job.id,'control');
   else if(runNode)addEdge(runNode.id,job.id,'control');
   if(planner?.applied_graph_version!=null){
    for(const task of selected)if(task.revision_graph_version===planner.applied_graph_version)addEdge(job.id,task.id,'control');
   }
  }else{
   const review=reviews.get(job.id),sources=review?.subject_task&&ids.has(review.subject_task)?[review.subject_task]:job.control_kind==='REVIEW'?leaves:[];
   if(sources.length)for(const id of sources)addEdge(id,job.id,'control');
   else if(runNode)addEdge(runNode.id,job.id,'control');
  }
 }
 const maxLevel=Math.max(0,...levels.values());
 const maxRows=Math.max(1,...rows.values());
 return {nodes:[...(runNode?[runNode]:[]),...controlNodes,...taskNodes],edges,width:Math.max(720,80+(maxLevel+1)*220,250+controlJobs.length*196),height:Math.max(520,138+maxRows*92)};
}
