const CONTROL_HEIGHT=54;
const TASK_WIDTH=174;
const TASK_HEIGHT=62;

export function layoutDag(tasks=[],dependencies=[],controlJobs=[],relations={}){
 const selected=tasks.filter(task=>task.selected!==false);
 const ids=new Set(selected.map(task=>task.id));
 const children=new Map(selected.map(task=>[task.id,[]]));
 const edges=[],edgeKeys=new Set();
 const addEdge=(upstream,downstream,kind='business')=>{const key=`${kind}:${upstream}:${downstream}`;if(upstream!==downstream&&!edgeKeys.has(key)){edgeKeys.add(key);edges.push({upstream,downstream,kind});}};
 for(const edge of dependencies){
  const upstream=edge.upstream_task_id,downstream=edge.downstream_task_id;
  if(!ids.has(upstream)||!ids.has(downstream)||upstream===downstream)continue;
  children.get(upstream).push(downstream);
  addEdge(upstream,downstream);
 }
 const runNode=controlJobs.length?{kind:'run',id:'__run__',item:relations.run||{},width:TASK_WIDTH,height:CONTROL_HEIGHT}:null;
 const controlNodes=controlJobs.map(job=>({kind:'control',id:job.id,item:job,width:TASK_WIDTH,height:CONTROL_HEIGHT}));
 const taskNodes=selected.map(task=>({kind:'task',id:task.id,item:task,width:TASK_WIDTH,height:TASK_HEIGHT}));
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
 const nodes=[...(runNode?[runNode]:[]),...controlNodes,...taskNodes],nodeIds=new Set(nodes.map(node=>node.id));
 const incoming=new Map(nodes.map(node=>[node.id,0])),outgoing=new Map(nodes.map(node=>[node.id,[]]));
 for(const edge of edges)if(nodeIds.has(edge.upstream)&&nodeIds.has(edge.downstream)){incoming.set(edge.downstream,incoming.get(edge.downstream)+1);outgoing.get(edge.upstream).push(edge.downstream);}
 const levels=new Map(nodes.map(node=>[node.id,0]));
 const queue=nodes.filter(node=>incoming.get(node.id)===0).sort((a,b)=>(a.kind==='run'?-2:a.kind==='control'?-1:0)-(b.kind==='run'?-2:b.kind==='control'?-1:0)).map(node=>node.id);
 for(let index=0;index<queue.length;index++){
  const id=queue[index];for(const child of outgoing.get(id)){levels.set(child,Math.max(levels.get(child),levels.get(id)+1));incoming.set(child,incoming.get(child)-1);if(incoming.get(child)===0)queue.push(child);}
 }
 // Historic malformed cycles stay inspectable at the end of the graph.
 let fallback=Math.max(0,...levels.values())+1;for(const node of nodes)if(incoming.get(node.id)>0)levels.set(node.id,fallback++);
 const groups=new Map();for(const node of nodes){const level=levels.get(node.id);if(!groups.has(level))groups.set(level,[]);groups.get(level).push(node);}
 let maxRows=1;for(const [level,group] of groups){group.sort((a,b)=>(a.kind==='control'?-1:a.kind==='run'?-2:0)-(b.kind==='control'?-1:b.kind==='run'?-2:0));maxRows=Math.max(maxRows,group.length);group.forEach((node,row)=>Object.assign(node,{x:36+level*220,y:62+row*104}));}
 const maxLevel=Math.max(0,...levels.values());return {nodes,edges,width:Math.max(760,90+(maxLevel+1)*220),height:Math.max(520,130+maxRows*104)};
}
