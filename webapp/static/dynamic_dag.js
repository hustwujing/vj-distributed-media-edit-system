const CONTROL_HEIGHT=54;
const TASK_WIDTH=174;
const TASK_HEIGHT=62;

export function layoutDag(tasks=[],dependencies=[],controlJobs=[]){
 const selected=tasks.filter(task=>task.selected!==false);
 const ids=new Set(selected.map(task=>task.id));
 const parents=new Map(selected.map(task=>[task.id,[]]));
 const children=new Map(selected.map(task=>[task.id,[]]));
 const edges=[];
 for(const edge of dependencies){
  const upstream=edge.upstream_task_id,downstream=edge.downstream_task_id;
  if(!ids.has(upstream)||!ids.has(downstream)||upstream===downstream)continue;
  parents.get(downstream).push(upstream);children.get(upstream).push(downstream);
  edges.push({upstream,downstream});
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
 const controlNodes=controlJobs.map((job,index)=>({kind:'control',id:job.id,item:job,x:116+index*196,y:30,width:TASK_WIDTH,height:CONTROL_HEIGHT}));
 const maxLevel=Math.max(0,...levels.values());
 const maxRows=Math.max(1,...rows.values());
 return {nodes:[...controlNodes,...taskNodes],edges,width:Math.max(720,80+(maxLevel+1)*220,140+controlJobs.length*196),height:Math.max(520,138+maxRows*92)};
}
