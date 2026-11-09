const CONTROL_HEIGHT=122;
const TASK_WIDTH=174;
const TASK_HEIGHT=122;

export function dagEdgePath(upstream,downstream){
 const down=downstream.y>=upstream.y+upstream.height;
 if(down&&upstream.band!==undefined&&downstream.band!==undefined&&upstream.band!==downstream.band){
  const x1=upstream.x+upstream.width/2,y1=upstream.y+upstream.height,x2=downstream.x+downstream.width/2,y2=downstream.y,middle=(y1+y2)/2;
  return `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`;
 }
 const right=downstream.x>=upstream.x+upstream.width,left=downstream.x+downstream.width<=upstream.x;
 if(right||left){
  const x1=upstream.x+(right?upstream.width:0),y1=upstream.y+upstream.height/2,x2=downstream.x+(left?downstream.width:0),y2=downstream.y+downstream.height/2,middle=(x1+x2)/2;
  return `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}`;
 }
 const x1=upstream.x+upstream.width/2,y1=upstream.y+(down?upstream.height:0),x2=downstream.x+downstream.width/2,y2=downstream.y+(down?0:downstream.height),middle=(y1+y2)/2;
 return `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`;
}

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
 const existedAt=(id,version)=>{const task=selected.find(item=>item.id===id);return version==null||task?.revision_graph_version==null||task.revision_graph_version<=version;};
 const leaves=selected.filter(task=>!children.get(task.id).length).map(task=>task.id);
 for(const job of controlJobs){
  if(job.control_kind==='PLAN'){
   const planner=planners.get(job.id),sources=[];
   for(const id of planner?.trigger_task_ids||[])if(ids.has(id)&&existedAt(id,planner.base_graph_version))sources.push(id);
   for(const id of planner?.trigger_control_job_ids||[])if(controlIds.has(id))sources.push(id);
   if(sources.length)for(const id of sources)addEdge(id,job.id,'control');
   else if(runNode)addEdge(runNode.id,job.id,'control');
   if(planner?.applied_graph_version!=null){
    for(const task of selected)if(task.revision_graph_version===planner.applied_graph_version)addEdge(job.id,task.id,'control');
   }
  }else{
   const review=reviews.get(job.id),sources=review?.subject_task?(ids.has(review.subject_task)&&existedAt(review.subject_task,review.graph_version)?[review.subject_task]:[]):job.control_kind==='REVIEW'?leaves.filter(id=>existedAt(id,review?.graph_version)):[];
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
 const width=Math.max(TASK_WIDTH+48,Math.floor(Number(relations.viewportWidth)||760));
 const columns=Math.max(1,Math.floor((width-48+46)/(TASK_WIDTH+46)));
 const pitch=columns>1?(width-48-TASK_WIDTH)/(columns-1):0;
 // A topology level may contain many reviews/parallel Tasks. Giving that
 // entire level one column made the right edge grow vertically forever.
 // Place every node in the next snake cell; edges retain real dependencies.
 const ordered=[...groups].sort(([a],[b])=>a-b).flatMap(([,group])=>group.sort((a,b)=>
  (a.kind==='run'?-2:a.kind==='control'?-1:0)-(b.kind==='run'?-2:b.kind==='control'?-1:0)));
 ordered.forEach((node,index)=>{
  const band=Math.floor(index/columns),column=index%columns,direction=band%2?-1:1;
  const x=columns===1?(width-TASK_WIDTH)/2:24+(direction===1?column:columns-1-column)*pitch;
  Object.assign(node,{x,y:62+band*164,level:levels.get(node.id),band,direction});
 });
 return {nodes,edges,width,height:Math.max(520,62+Math.ceil(nodes.length/columns)*164),columns};
}
