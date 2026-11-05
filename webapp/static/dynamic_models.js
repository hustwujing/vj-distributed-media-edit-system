export const effortLabels={low:'低',medium:'中',high:'高',max:'最高',xhigh:'更高',minimal:'最小',none:'关闭'};

export function chooseModel(choices,previous=null){
 const available=(choices?.profiles||[]).filter(p=>p.available);
 const profile=available.find(p=>p.id===previous?.profile)||available.find(p=>p.id===choices?.default_profile);
 if(!profile)return null;
 const effort=profile.reasoning_efforts.includes(previous?.reasoning_effort)?previous.reasoning_effort:profile.default_effort;
 return {profile:profile.id,reasoning_effort:effort,profile_digest:profile.profile_digest};
}

export function choiceIsCurrent(choices,choice){
 const profile=choices?.profiles?.find(p=>p.id===choice?.profile);
 return !!profile?.available&&profile.profile_digest===choice.profile_digest&&profile.reasoning_efforts.includes(choice.reasoning_effort);
}

export function configurationText(configuration){
 if(!configuration)return '本轮模型配置未记录';
 const labels={media_planner:'Manager',editing_implementer:'Implementer',visual_quality_reviewer:'Task Reviewer',media_failure_diagnostician:'Diagnostician',delivery_reviewer:'Final Reviewer'};
 const roles=Object.entries(configuration.roles||{}).map(([role,value])=>`${labels[role]||role}=${value.model}/${effortLabels[value.reasoning_effort]||value.reasoning_effort}`);
 return `${configuration.model} · 推理强度：${effortLabels[configuration.reasoning_effort]||configuration.reasoning_effort}${roles.length?' · '+roles.join('，'):''}`;
}
