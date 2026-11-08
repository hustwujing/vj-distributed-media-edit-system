import {esc,href,toast} from './common.js';

export function compareVideos(items){
 const videos=items.filter(x=>x.type?.startsWith('video/')).slice(0,8);
 if(videos.length<2){toast('至少需要两条已完成的视频结果才能对比');return;}
 const dialog=document.createElement('dialog');dialog.className='video-compare';
 dialog.innerHTML=`<header><div><h2>同步对比版本</h2><p>两个播放器共用进度；可选择要保留的版本。</p></div><button class="icon-button" aria-label="关闭">×</button></header><div class="video-compare-select"><label>左侧 <select></select></label><label>右侧 <select></select></label></div><div class="video-compare-stage"><video controls playsinline preload="metadata"></video><video controls playsinline preload="metadata"></video></div><footer><span class="compare-time">0:00</span><button class="button secondary" data-play>同步播放</button><button class="button primary" data-keep>将右侧设为当前预览</button></footer>`;
 document.body.append(dialog);dialog.showModal();
 const $$=s=>[...dialog.querySelectorAll(s)],$=s=>dialog.querySelector(s),[left,right]=$$('video'),[a,b]=$$('select');
 const option=(item,selected)=>`<option value="${esc(item.url)}" ${selected?'selected':''}>${esc(item.name)}</option>`;
 a.innerHTML=videos.map((v,i)=>option(v,!i)).join('');b.innerHTML=videos.map((v,i)=>option(v,i===1)).join('');
 let syncing=false;
 const choose=(video,select)=>{video.src=href(select.value);video.load();};choose(left,a);choose(right,b);
 a.onchange=()=>choose(left,a);b.onchange=()=>choose(right,b);
 const sync=(origin,target)=>{if(syncing||!Number.isFinite(origin.currentTime))return;syncing=true;if(Math.abs(target.currentTime-origin.currentTime)>.12)target.currentTime=origin.currentTime;syncing=false;$('.compare-time').textContent=`${Math.floor(origin.currentTime/60)}:${String(Math.floor(origin.currentTime%60)).padStart(2,'0')}`;};
 left.ontimeupdate=()=>sync(left,right);right.ontimeupdate=()=>sync(right,left);
 left.onplay=()=>right.play().catch(()=>{});right.onplay=()=>left.play().catch(()=>{});left.onpause=()=>right.pause();right.onpause=()=>left.pause();
 $('[data-play]').onclick=()=>{if(left.paused){left.play().catch(()=>{});}else left.pause();};
 $('[data-keep]').onclick=()=>{const item=videos.find(x=>x.url===b.value);dialog.dispatchEvent(new CustomEvent('keep',{detail:item}));dialog.close();};
 dialog.querySelector('header button').onclick=()=>dialog.close();dialog.addEventListener('close',()=>{
  for(const video of [left,right]){video.onplay=video.onpause=video.ontimeupdate=null;video.pause();video.removeAttribute('src');video.load();}
  dialog.remove();
 },{once:true});
 return dialog;
}
