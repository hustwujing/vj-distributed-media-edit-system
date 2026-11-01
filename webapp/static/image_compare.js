import {esc,toast} from './common.js';

// Both layers share one sized stage, so zoom and scrolling always stay aligned.
export async function openCompare({title='对比效果',before,candidates,onApply}){
 if(!candidates.length)return toast('暂无可对比结果');
 const dialog=document.createElement('dialog');dialog.className='image-compare';
 dialog.innerHTML=`<header><div><h2>${esc(title)}</h2><p class="compare-caption"></p></div><button class="icon-button compare-close" aria-label="关闭对比">×</button></header><div class="compare-controls"><span>左侧：${esc(before.name||'修改前')} · 右侧：所选效果</span><label>缩放 <select aria-label="对比缩放"><option value="fit">适应</option><option value="1">100%</option><option value="2">200%</option></select></label></div><div class="compare-viewport"><div class="compare-stage"><img class="compare-before" alt="修改前"><img class="compare-after" alt="所选效果"><div class="compare-divider"></div></div></div><label class="compare-slider-label">拖动分隔线查看前后变化<input class="compare-slider" type="range" min="0" max="100" value="50" aria-label="前后对比分隔位置"></label><div class="compare-candidates" aria-label="候选效果"></div><footer><span class="compare-error" role="alert"></span><button class="button primary compare-apply" disabled ${onApply?'':'hidden'}>应用所选效果</button></footer>`;
 document.body.append(dialog);dialog.showModal();
 const $=s=>dialog.querySelector(s),viewport=$('.compare-viewport'),stage=$('.compare-stage'),beforeImg=$('.compare-before'),afterImg=$('.compare-after');
 let choice=candidates[0],pending=false,sequence=0,beforeReady=false;
 const close=()=>{if(!pending)dialog.close();};$('.compare-close').onclick=close;
 dialog.addEventListener('cancel',e=>{if(pending)e.preventDefault();});
 dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)close();}});
 const fit=()=>{const w=beforeImg.naturalWidth||afterImg.naturalWidth||640,h=beforeImg.naturalHeight||afterImg.naturalHeight||480;const zoom=$('select').value;const scale=zoom==='fit'?Math.max(.02,Math.min((viewport.clientWidth-32)/w,(viewport.clientHeight-32)/h,1)):Number(zoom);stage.style.width=w*scale+'px';stage.style.height=h*scale+'px';};
 const observer=new ResizeObserver(fit);observer.observe(viewport);dialog.addEventListener('close',()=>{observer.disconnect();dialog.remove();},{once:true});
 $('select').onchange=fit;
 const slide=()=>{const v=$('.compare-slider').value;afterImg.style.clipPath=`inset(0 0 0 ${v}%)`;$('.compare-divider').style.left=v+'%';};$('.compare-slider').oninput=slide;slide();
 stage.onpointerdown=e=>{stage.setPointerCapture(e.pointerId);position(e);};stage.onpointermove=e=>{if(stage.hasPointerCapture(e.pointerId))position(e);};
 function position(e){const r=stage.getBoundingClientRect();$('.compare-slider').value=Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100));slide();}
 $('.compare-candidates').innerHTML=candidates.map((c,i)=>`<button data-candidate="${i}" aria-pressed="${i===0}"><img src="${esc(c.url)}" alt=""><span>${esc(c.name)}</span></button>`).join('');
 async function choose(c){const token=++sequence;choice=c;$('.compare-apply').disabled=true;$('.compare-caption').textContent=c.name;$('.compare-error').textContent='';
  $('.compare-candidates').querySelectorAll('button').forEach((b,i)=>b.setAttribute('aria-pressed',String(candidates[i]===c)));
  try{const url=c.preview?await c.preview():c.url;if(token!==sequence)return;afterImg.src=url;await afterImg.decode();if(token===sequence){fit();$('.compare-apply').disabled=pending||!beforeReady;}}catch{$('.compare-error').textContent='图片无法加载，请重试';}}
 $('.compare-candidates').querySelectorAll('button').forEach(b=>b.onclick=()=>{if(!pending)choose(candidates[+b.dataset.candidate]);});
 $('.compare-apply').onclick=async()=>{if(pending)return;pending=true;$('.compare-apply').disabled=true;$('.compare-close').disabled=true;$('.compare-apply').textContent='正在应用…';try{await onApply(choice);pending=false;dialog.close();}catch(e){$('.compare-error').textContent=e.message;pending=false;$('.compare-apply').disabled=false;$('.compare-close').disabled=false;$('.compare-apply').textContent='应用所选效果';}};
 beforeImg.src=before.url;try{await beforeImg.decode();beforeReady=true;await choose(choice);}catch{$('.compare-error').textContent='原图无法加载';}fit();
 return dialog;
}
