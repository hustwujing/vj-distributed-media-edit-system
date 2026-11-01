// Pure presentation policy: shared by chat cards, the result library and tests.
export function processArtifact(item){
 if(item.workflow_delivery===false)return true;
 if(['pending','superseded'].includes(item.quality_state))return true;
 if(typeof item.is_process==='boolean')return item.is_process;
 if(item.role==='deliverable')return false;
 const name=item.name||'';
 return /^(?:base(?:_|\.)|source_|clips?_|animations\/|tmp\/|temp\/)/i.test(name)||/^(?:edl|edit_plan|project|title|(?:demo-)?plan|deliverables)\.(?:json|md|png)$/i.test(name)||/\.py$|\.report\.json$|\.verify_\d+\.jpe?g$/i.test(name);
}
export function revisionOf(name){
 const match=(name||'').match(/^(.*?)[-_. ]v(?:ersion)?[-_. ]?(\d+)(\.[^/.]+)$/i);
 return match?{family:(match[1]+match[3]).toLowerCase(),number:Number(match[2])}:{family:(name||'').toLowerCase(),number:1};
}
const mediaRank=item=>item.type?.startsWith('video/')?0:item.type?.startsWith('audio/')?1:item.type?.startsWith('image/')?2:3;
function compare(a,b){
 return Number(processArtifact(a))-Number(processArtifact(b))||Number(!a.is_primary)-Number(!b.is_primary)||mediaRank(a)-mediaRank(b)||
  (Number(b.source_modified)||0)-(Number(a.source_modified)||0)||
  Number(!/^final(?:[-_. ]v(?:ersion)?[-_. ]?\d+)?\./i.test(a.name?.split('/').pop()||''))-Number(!/^final(?:[-_. ]v(?:ersion)?[-_. ]?\d+)?\./i.test(b.name?.split('/').pop()||''))||
  revisionOf(b.name).number-revisionOf(a.name).number||(a.name||'').localeCompare(b.name||'',undefined,{numeric:true});
}
export function orderedArtifacts(artifacts,text=''){
 const items=artifacts.map(a=>({...a}));const candidates=items.filter(a=>!processArtifact(a));
 let primary=candidates.find(a=>a.is_primary);
 if(!primary&&text){const mentioned=candidates.filter(a=>{const name=a.name.split('/').pop().replace(/[.*+?^${}()|[\]\\]/g,'\\$&');return new RegExp('(^|[^\\w.-])'+name+'($|[^\\w.-])','i').test(text);});if(mentioned.length===1)primary=mentioned[0];}
 primary ||= [...candidates].sort(compare)[0];
 for(const item of items){item.is_primary=item===primary;item.alternate_version=!!primary&&item!==primary&&revisionOf(item.name).family===revisionOf(primary.name).family&&item.role!=='deliverable';}
 return items.sort(compare);
}
export function collectResults(project,turns,labels,formatDate){
 const groups=[...project.jobs.filter(j=>j.status==='succeeded').map(j=>({...j,origin:'job',group:labels[j.kind]})),
  ...turns.filter(t=>(t.status==='succeeded'||t.workflow)&&t.artifacts.length).map(t=>({...t,origin:'agent',group:t.mode==='plan'?'样式预览':'Agent 生成'}))]
  .sort((a,b)=>(b.updated||b.created)-(a.updated||a.created));
 const items=[],seen=new Set();
 for(const group of groups){for(const a of orderedArtifacts(group.artifacts,group.text||'')){
  // Agent turns used to export the entire edit directory repeatedly. Show its
  // latest file snapshot once; earlier snapshots stay in their original messages.
  const key=group.origin==='agent'&&a.purpose!=='style_preview'?'agent:'+a.name:group.origin+':'+group.id+':'+a.name;
  if(seen.has(key))continue;seen.add(key);
  items.push({...a,group_id:group.id,origin:group.origin,created:group.updated||group.created,
   subtitle:`${group.group} · ${formatDate(group.updated||group.created)}`});
 }}
 return items;
}
export function resultSections(items){
 const visible=items.filter(a=>!processArtifact(a));
 const primary=visible.find(a=>a.is_primary&&/^(video|audio)\//.test(a.type))||visible.find(a=>/^(video|audio)\//.test(a.type)&&!a.alternate_version)||visible.find(a=>a.is_primary)||visible[0];
 const current=visible.filter(a=>a!==primary&&!a.alternate_version&&a.created>=primary?.created);
 return {primary,current,history:visible.filter(a=>a!==primary&&!current.includes(a)),process:items.filter(processArtifact)};
}
