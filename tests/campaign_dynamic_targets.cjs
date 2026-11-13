function targetText(spec){
 if(spec.delivery_target_text)return spec.delivery_target_text;
 const e=spec.expect;
 if(e.kind==='image')return `符合需求的 ${e.width}×${e.height} 图片${e.min_images>1?'及全部要求的图片版本':''}${e.extra_alpha?'，同时交付透明背景 PNG':''}${e.extra_extension?'，包含 '+e.extra_extension+' 分层文件':''}`;
 if(e.kind==='video')return `符合需求的完整 ${e.duration} 秒 ${e.width}×${e.height} 视频及要求的附加文件`;
 if(e.kind==='video_variants')return '符合需求的所有画幅视频及要求的附加文件';
 if(e.kind==='audio')return `符合需求的 ${e.duration} 秒音频及要求的格式版本`;
 if(e.kind==='text')return '符合需求的 '+e.extensions.join(' 和 ')+' 文件';
 return '符合本次要求的成果与可核实的处理记录';
}

const GATED_INTERVENTIONS=new Set(['R01','R02','R03','R11','R15','R17']);
const initialPlanApprovalRequired=spec=>GATED_INTERVENTIONS.has(spec.id);
const revisionDecisionKinds=()=>new Set(['plan_confirmation']);
const nextRevisionDecision=decisions=>decisions.find(item=>item.kind==='plan_change')||decisions.find(item=>item.kind==='plan_confirmation')||decisions[0];
function deliveryAmendmentMatches(existing,request,expectedFilenames=[]){
 if(!request.delivery_scope_changed)return expectedFilenames.length===0&&JSON.stringify(request.delivery_targets)===JSON.stringify(existing);
 if(JSON.stringify(request.previous_delivery_targets)!==JSON.stringify(existing))return false;
 if(!existing.every(target=>request.delivery_targets.includes(target)))return false;
 const added=request.delivery_targets.filter(target=>!existing.includes(target));
 return added.length===expectedFilenames.length&&expectedFilenames.every(name=>
  added.some(target=>target.includes(name))&&(request.delivery_contract?.filenames||[]).includes(name));
}

module.exports={targetText,initialPlanApprovalRequired,revisionDecisionKinds,nextRevisionDecision,deliveryAmendmentMatches};
