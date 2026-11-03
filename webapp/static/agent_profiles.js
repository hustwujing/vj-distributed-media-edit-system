import {api,esc,toast} from './common.js';
import {randomRequestId} from './request_id.js';

const labels={requested:'已请求切换',waiting:'等待安全切换点',blocked:'切换待核实',activated:'后续 Agent 执行已切换',next_turn:'下一轮使用新 Agent',cancelled:'切换已取消',failed:'切换未完成',unchanged:'已使用此配置'};
export function profileStatus(turn){
 const p=turn?.agent_profile,s=turn?.harness_switch;
 return {current:p?`${p.harness} · ${p.model} · ${p.reasoning_effort}`:'',
  state:s?(labels[s.state]||'未知切换状态'):'',reason:s?.reason||'',
  canCancel:!!s&&['requested','waiting','blocked'].includes(s.state)};
}

export function mountAgentProfiles(root,project,readTurns,refresh){
 if(!root)return {load:async()=>{},update:()=>{}};
 root.innerHTML='<summary>Agent 设置</summary><div class="agent-profile-controls"><label>此项目的 Agent <select aria-label="选择 Agent 配置"></select></label><button type="button" class="button secondary">应用</button><button type="button" class="button subtle" data-cancel hidden>撤销切换</button><p role="status" aria-live="polite"></p><small>活动任务在安全边界交接，已有成果和工作流进度保留。原任务的调用额度不会重置。</small></div>';
 const select=root.querySelector('select'),apply=root.querySelector('button'),cancel=root.querySelector('[data-cancel]'),notice=root.querySelector('[role=status]');
 let busy=false,loaded=false,lastTurn=null;
 function update(){
  const turns=readTurns(),turn=turns.find(t=>['queued','running','waiting','paused','cancelling'].includes(t.status))||turns.at(-1);
  lastTurn=turn;const view=profileStatus(turn);
  notice.textContent=[view.current,view.state,view.reason].filter(Boolean).join(' · ');
  cancel.hidden=!view.canCancel;cancel.disabled=busy;apply.disabled=busy||!loaded||!select.value||view.canCancel||turn?.status==='cancelling';select.disabled=busy;
 }
 async function load(){
  const data=await api(`api/projects/${project}/agent/profiles`);
  select.innerHTML=data.profiles.map(p=>`<option value="${esc(p.name)}" ${p.available?'':'disabled'}>${esc(p.name+' · '+p.harness+' · '+p.model+(p.available?'':'（未配置或未安装）'))}</option>`).join('');
  select.value=data.selected;loaded=true;update();
 }
 apply.onclick=async()=>{
  if(busy)return;busy=true;update();
  try{
   const active=readTurns().find(t=>['queued','running','waiting','paused'].includes(t.status));
   if(active){
    await api(`api/projects/${project}/agent/${active.id}/harness-switch`,{method:'POST',body:JSON.stringify({profile:select.value,request_key:randomRequestId(),expected_digest:active.agent_profile.digest})});
    toast('已请求在安全边界切换 Agent。');
   }else{
    await api(`api/projects/${project}/agent/profile`,{method:'PUT',body:JSON.stringify({profile:select.value})});
    toast('后续任务将使用所选 Agent。');
   }
   await refresh();
  }catch(error){toast(error.message,true);}finally{busy=false;update();}
 };
 cancel.onclick=async()=>{
  if(busy||!lastTurn?.harness_switch)return;busy=true;update();
  try{await api(`api/projects/${project}/agent/${lastTurn.id}/harness-switch/${lastTurn.harness_switch.id}`,{method:'DELETE'});await load();await refresh();}
  catch(error){toast(error.message,true);}finally{busy=false;update();}
 };
 select.onchange=update;
 return {load,update};
}
