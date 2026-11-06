import {processArtifact,orderedArtifacts,collectResults,resultSections} from './artifact_order.js';
export {orderedArtifacts};
export const $=id=>document.getElementById(id);
export const base=new URL(document.querySelector('base').href);
export const href=path=>new URL(path,base).href;
export const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const labels={transcribe:'语音转写',pack:'素材笔记',timeline:'时间线预览',analyze:'画面分析',review:'成片质检',render:'视频合成',edit:'音视频处理',image:'图片处理'};
export const states={queued:'排队中',running:'处理中',succeeded:'已完成',failed:'未完成',cancelled:'已停止',cancelling:'正在停止',waiting:'后台处理中',paused:'已暂停'};
const paths={home:'M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',arrow:'m14 5-7 7 7 7M7 12h14',plus:'M12 5v14M5 12h14',close:'m6 6 12 12M6 18 18 6',play:'m9 5 11 7-11 7z',download:'M12 3v12m-5-5 5 5 5-5M5 17v4h14v-4',upload:'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',film:'M3 4h18v16H3zM7 4v16M17 4v16M3 9h4m10 0h4M3 15h4m10 0h4',spark:'m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8z',tools:'M4 7h16M4 17h16M8 4v6m8 4v6',check:'m5 12 4 4L20 5',clock:'M12 8v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18',image:'M3 3h18v18H3zM3 17l6-6 4 4 3-3 5 5M8 7h.01',file:'M5 3h9l5 5v13H5zM14 3v5h5M8 12h8m-8 4h6',chevron:'m8 5 7 7-7 7',stop:'M6 6h12v12H6z',send:'m4 12 8-8 8 8M12 4v16',expand:'M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5',attach:'m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9',search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6',chat:'M3 3h18v14H8l-5 4zM7 7h10M7 12h7',code:'m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18',eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6',undo:'m8 3-5 5 5 5M3 8h11a7 7 0 0 1 0 14',dots:'M5 12h.01M12 12h.01M19 12h.01'};
export function icon(name,size=18){return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name]||paths.file}"/></svg>`;}
export const editorHref=(mode,id)=>href(`${mode}/${id}`);
export const activeStatus=s=>['queued','running','waiting','paused','cancelling'].includes(s);
export function duration(seconds){const n=Math.max(0,Math.floor(seconds||0));return `${Math.floor(n/60).toString().padStart(2,'0')}:${(n%60).toString().padStart(2,'0')}`;}
export function bytes(size){return size>1024**2?`${(size/1024**2).toFixed(1)} MB`:`${Math.round((size||0)/1024)} KB`;}
export function date(timestamp){return new Date(timestamp*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
export function errorText(data,status,requestId){const detail=Array.isArray(data?.detail)?data.detail.map(x=>x.msg).join('；'):data?.detail||data?.error;return ((typeof detail==='object'?detail?.message:detail)||`请求失败（${status}）`)+(requestId?` · 查询编号：${requestId}`:'');}
export async function api(path,options={}){
 const response=await fetch(href(path),{credentials:'same-origin',...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
 const data=await response.json().catch(()=>({}));
 if(!response.ok)throw new Error(errorText(data,response.status,response.headers.get('X-Request-ID')));
 return data;
}
let toastTimeout;
export function toast(text,error=false){const host=document.querySelector('dialog[open]')||document.body;let box=$('toast');if(!box){box=document.createElement('div');box.id='toast';box.setAttribute('role',error?'alert':'status');}if(box.parentElement!==host)host.append(box);box.setAttribute('role',error?'alert':'status');box.className='toast'+(error?' error':'');box.textContent=text;box.hidden=false;clearTimeout(toastTimeout);toastTimeout=setTimeout(()=>box.hidden=true,error?9000:3200);}
export function on(id,callback){$(id)?.addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{await callback(event);}catch(error){toast(error.message,true);}finally{button.disabled=false;}});}
export function header(mode='home',project=null){
 $('app-header').innerHTML=`<a class="brand" href="${href('')}" aria-label="updream-AI 编辑 首页"><span class="brand-mark">${icon('play',17)}</span><b>updream-AI <span>编辑</span></b></a>${project?`<span class="header-divider"></span><a class="back-home" href="${href('')}">${icon('arrow',15)}示例首页</a><span class="project-title" title="${esc(project.name)}">${esc(project.name)}</span><nav class="editor-switch"><a data-mode-link href="${editorHref('images',project.id)}" class="${mode==='images'?'selected':''}">${icon('image',15)}图片编辑</a><a data-mode-link href="${editorHref('tools',project.id)}" class="${mode==='tools'?'selected':''}">${icon('tools',15)}工具编辑</a><a data-mode-link href="${editorHref('chat',project.id)}" class="${mode==='chat'?'selected':''}">${icon('spark',15)}聊天编辑</a></nav>`:`<nav class="home-nav"><a class="selected" href="${href('')}">示例库</a><a href="#recent-projects">我的项目</a></nav>`}<div class="header-end"><span class="private-badge">${icon('file',13)}私有工作区</span><button class="button ${project?'subtle':'primary'}" id="new-project">${icon('plus',15)}新建项目</button></div>`;
 on('new-project',()=>newProject(mode==='images'?'images':mode==='tools'?'tools':'chat'));
}
function dialog(id){let node=$(id);if(!node){node=document.createElement('dialog');node.id=id;document.body.append(node);node.addEventListener('click',event=>{if(event.target===node){const rect=node.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)node.close();}});node.addEventListener('close',()=>node.querySelectorAll('video,audio').forEach(v=>v.pause()));}return node;}
export async function newProject(mode='chat'){
 const modal=dialog('project-modal');modal.className='project-modal';modal.innerHTML=`<button class="modal-close" aria-label="关闭">${icon('close')}</button><div class="eyebrow">NEW PROJECT</div><h2>开始一个新作品</h2><p class="muted">选择适合你的编辑方式，之后可以随时切换。</p><form id="new-project-form"><label class="field">项目名称<input name="name" required maxlength="100" placeholder="给这次创作起个名字" value="我的创作项目"></label><div class="mode-choices"><label><input type="radio" name="mode" value="images" ${mode==='images'?'checked':''}><span>${icon('image',23)}<strong>图片编辑</strong><small>画布、图层与 AI 修图</small></span></label><label><input type="radio" name="mode" value="tools" ${mode==='tools'?'checked':''}><span>${icon('tools',23)}<strong>工具编辑</strong><small>精确控制片段与参数</small></span></label><label><input type="radio" name="mode" value="chat" ${mode==='chat'?'checked':''}><span>${icon('spark',23)}<strong>聊天编辑</strong><small>描述想法，让 Agent 完成</small></span></label></div><button class="button primary wide" type="submit">创建并进入编辑器 ${icon('chevron',15)}</button></form>`;
 modal.querySelector('.modal-close').onclick=()=>modal.close();
 modal.querySelector('form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=event.submitter;button.disabled=true;try{const p=await api('api/projects',{method:'POST',body:JSON.stringify({name:form.elements.name.value.trim()})});location.assign(editorHref(form.elements.mode.value,p.id));}catch(error){toast(error.message,true);button.disabled=false;}};
 modal.showModal();modal.querySelector('input[name=name]').select();
}
export function markdown(text,artifacts=[]){
 const code=[];let escaped=esc(text).replace(/```([^\n]*)\n?([\s\S]*?)```/g,(_,language,body)=>{code.push(`<pre><code>${body}</code></pre>`);return `\u0000${code.length-1}\u0000`;});
 escaped=escaped.replace(/(!?)\[([^\]\n]+)\]\(([^)\n]+)\)/g,(_,picture,label,target)=>{
  target=target.trim();
  if(target.startsWith('&lt;')&&target.endsWith('&gt;'))target=target.slice(4,-4);
  const item=artifacts.find(a=>(a.url&&target===esc(a.url))||(a.name&&(target.endsWith('/'+esc(a.name))||target===esc(a.name))));
  if(picture&&item&&(isPreviewImage(item)||item.type?.startsWith('video/'))){code.push(mediaCard(item,{compact:true}));return `\u0000${code.length-1}\u0000`;}
  if(item)return `<button type="button" class="inline-media-link" data-media-url="${esc(item.url)}">${label}</button>`;
  if(/^https?:\/\//i.test(target))return `<a href="${target}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  return label;
 });
 escaped=escaped.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`\n]+)`/g,'<code>$1</code>');
 return escaped.split(/\n\n+/).map(block=>{
  if(/^\u0000\d+\u0000$/.test(block.trim()))return code[Number(block.replace(/\u0000/g,''))];
  if(/^#{1,3}\s/.test(block))return `<h4>${block.replace(/^#{1,3}\s+/,'')}</h4>`;
  const lines=block.split('\n');if(lines.every(line=>/^\s*[-*]\s/.test(line)))return '<ul>'+lines.map(line=>`<li>${line.replace(/^\s*[-*]\s/,'')}</li>`).join('')+'</ul>';
  if(lines.every(line=>/^\s*\d+[.)]\s/.test(line)))return '<ol>'+lines.map(line=>`<li>${line.replace(/^\s*\d+[.)]\s/,'')}</li>`).join('')+'</ol>';
  return `<p>${block.replace(/\n/g,'<br>')}</p>`;
 }).join('').replace(/\u0000(\d+)\u0000/g,(_,index)=>code[+index]);
}
export const isPreviewImage=item=>item.type?.startsWith('image/')&&!/\.(psd|tiff?|ora)$/i.test(item.name);
export function mediaCard(item,{compact=false}={}){
 if(item.source_step&&item.workflow_delivery!==true)item={...item,subtitle:'步骤成果 · 非最终交付'};
 if(item.quality_state==='pending')item={...item,subtitle:'候选结果 · 尚未验收'};
 if(item.quality_state==='superseded')item={...item,subtitle:'历史候选 · 已被修复版本替代'};
 const video=item.type?.startsWith('video/'),image=isPreviewImage(item);const poster=image?item.url:item.poster_url;
 if(!video&&!image)return `<button class="document-card" data-media-url="${esc(item.url)}">${icon('file',20)}<span>${esc(item.name)}</span><small>${item.type?.startsWith('audio/')?'试听':'查看'}</small></button>`;
 return `<button class="media-card ${compact?'compact':''}" data-media-url="${esc(item.url)}" aria-label="预览 ${esc(item.name)}"><span class="media-cover">${poster?`<img src="${href(poster)}" loading="lazy" alt="${esc(item.name)}">`:`<span class="media-placeholder">${icon(video?'film':'image',30)}</span>`}<span class="media-play">${icon(video?'play':'expand',18)}</span><span class="media-kind">${video?'视频':'图片'}</span></span><span class="media-caption"><strong>${esc(item.name)}</strong><small>${item.subtitle?esc(item.subtitle):video?'点击播放':'点击放大'}</small></span></button>`;
}
export function bindMedia(container,items,callback=openMedia){const lookup=new Map(items.map(item=>[item.url,item]));container.querySelectorAll('[data-media-url]').forEach(button=>button.onclick=()=>{const item=lookup.get(button.dataset.mediaUrl);if(item)callback(item);});}
export function sourceItems(project){return project.assets.map(a=>({...a,type:a.kind==='video'?'video/mp4':a.kind==='image'?(a.mime||'image/png'):a.kind==='audio'?(a.mime||'audio/wav'):a.kind==='document'?(a.mime||'application/octet-stream'):'text/plain',subtitle:a.kind==='video'?`${duration(a.duration)} · ${a.width} × ${a.height}`:a.kind==='image'?`${a.width} × ${a.height} · 图片 · 点击放大`:a.kind==='audio'?`${duration(a.duration)} · 音频 · 点击试听`:a.kind==='document'?`${Math.max(1,Math.ceil((a.size||0)/1024))} KB · 参考资料`:'字幕文件'}));}
export const isProcessFile=processArtifact;
export function resultItems(project,turns=[]){return collectResults(project,turns,labels,date);}
export class Preview{
 constructor(container,{modal=false}={}){this.container=container;this.current=null;this.token=0;this.modal=modal;container.innerHTML=`<div class="preview-top"><div><span class="eyebrow">PREVIEW</span><h3 class="preview-name">作品预览</h3></div><button class="icon-button preview-expand" title="放大预览">${icon('expand')}</button></div><div class="preview-stage"><div class="preview-empty">${icon('film',38)}<h3>在这里，先看效果</h3><p>选择素材或生成结果即可预览</p></div></div><div class="preview-bottom"><span class="preview-info">音视频、图片和文本都可以直接预览</span><a class="button subtle preview-download" hidden> ${icon('download',15)}下载</a></div>`;container.querySelector('.preview-expand').onclick=()=>{if(this.current)openMedia(this.current);};if(modal)container.querySelector('.preview-expand').hidden=true;}
 async show(item,{autoplay=false}={}){
  if(!item)return;if(this.current?.url===item.url){if(autoplay)this.container.querySelector('video')?.play().catch(()=>{});return;}
  this.current=item;const token=++this.token;const stage=this.container.querySelector('.preview-stage');stage.querySelectorAll('video,audio').forEach(v=>v.pause());
  this.container.querySelector('.preview-name').textContent=item.name;this.container.querySelector('.preview-info').textContent=item.subtitle||'预览完成后可下载原文件';
  const download=this.container.querySelector('.preview-download');download.href=href(item.url+(item.url.includes('?')?'&':'?')+'download=true');download.download=item.name;download.hidden=false;
  if(item.type?.startsWith('video/')){
   stage.innerHTML=`<video controls playsinline preload="metadata" ${item.poster_url?`poster="${href(item.poster_url)}"`:''} src="${href(item.url)}"></video>`;const video=stage.querySelector('video');video.onloadedmetadata=()=>{if(this.current?.url===item.url)this.container.querySelector('.preview-info').textContent=`${duration(video.duration)} · ${video.videoWidth} × ${video.videoHeight}`;};video.onerror=()=>toast('浏览器无法播放此编码，可下载原文件查看。',true);if(autoplay)video.play().catch(()=>{});
  }else if(isPreviewImage(item))stage.innerHTML=`<img class="preview-image" src="${href(item.url)}" alt="${esc(item.name)}">`;
  else if(item.type==='application/pdf')stage.innerHTML=`<iframe class="preview-document" src="${href(item.url)}" title="预览 ${esc(item.name)}"></iframe>`;
  else if(item.type?.startsWith('audio/'))stage.innerHTML=`<audio controls src="${href(item.url)}"></audio>`;
  else if(/\.(psd|ora|zip|tiff|docx|xlsx|pptx)$/i.test(item.name))stage.innerHTML='<div class="preview-empty"><h3>文件已就绪</h3><p>可下载后在兼容的应用中查看。</p></div>';
  else{stage.innerHTML='<div class="loading-state"><span class="spinner"></span>正在读取内容…</div>';try{const response=await fetch(href(item.url));if(!response.ok)throw new Error('无法读取文件');let text=await response.text();if(item.type==='application/json'){try{text=JSON.stringify(JSON.parse(text),null,2);}catch{}}if(token===this.token)stage.innerHTML=`<pre class="text-preview">${esc(text.slice(0,180000))}</pre>`;}catch(error){if(token===this.token)stage.innerHTML=`<p class="inline-error">${esc(error.message)}</p>`;}}
 }
}
export function openMedia(item){const modal=dialog('media-modal');modal.className='media-modal';modal.innerHTML=`<button class="modal-close" aria-label="关闭预览">${icon('close')}</button><div id="modal-preview"></div>`;modal.querySelector('.modal-close').onclick=()=>modal.close();const preview=new Preview(modal.querySelector('#modal-preview'),{modal:true});modal.showModal();preview.show(item,{autoplay:true});}
export function showText(title,text){const modal=dialog('text-modal');modal.className='text-modal';modal.innerHTML=`<button class="modal-close" aria-label="关闭">${icon('close')}</button><h2>${esc(title)}</h2><pre class="text-preview">${esc(text)}</pre>`;modal.querySelector('.modal-close').onclick=()=>modal.close();modal.showModal();}
export class ResultBrowser{
 constructor(container,preview){this.container=container;this.preview=preview;this.signature=null;this.items=[];this.primary=null;}
 update(items){const sig=JSON.stringify(items.map(a=>[a.url,a.is_primary,a.alternate_version,a.quality_state,a.workflow_delivery,a.is_process,a.source_step,a.role,a.subtitle]));if(sig===this.signature)return;this.signature=sig;const oldFirst=this.items[0]?.url,oldPrimary=this.primary?.url;this.items=items;
  const row=item=>`<button class="result-row" data-result="${esc(item.url)}"><span class="result-thumb">${item.poster_url||isPreviewImage(item)?`<img loading="lazy" src="${href(item.poster_url||item.url)}" alt="">`:icon(item.type?.startsWith('video/')?'film':'file',19)}</span><span><strong>${esc(item.name)}</strong><small>${esc(item.subtitle||'')}</small></span>${icon('chevron',14)}</button>`;
  const {primary,current,history,process}=resultSections(items);this.primary=primary;
  const label=primary?.type?.startsWith('video/')?'最新成片':primary?.type?.startsWith('audio/')?'最新音频':'最新预览';
  this.container.innerHTML=items.length?(primary?`<section class="latest-result"><div class="latest-result-label">${icon('check',14)}${label}<small>点击查看</small></div>${row(primary)}</section>`:'')+
   current.map(row).join('')+(history.length?`<details class="more-results history-results"><summary>历史版本与预览（${history.length}）</summary>${history.map(row).join('')}</details>`:'')+
   (process.length?`<details class="more-results process-results"><summary>脚本与过程文件（${process.length}）</summary>${process.map(row).join('')}</details>`:''):'<div class="empty-small">生成的成片和检查图会出现在这里</div>';
  this.container.querySelectorAll('[data-result]').forEach(button=>button.onclick=()=>this.select(items.find(item=>item.url===button.dataset.result)));
  if(primary){
   const newest=items.find(a=>!isProcessFile(a)&&!a.alternate_version&&/^(video|image|audio)\//.test(a.type));
   if(!this.preview.current||oldPrimary!==primary.url)this.select(primary);
   else if(oldFirst&&oldFirst!==items[0]?.url&&newest?.created>primary.created)this.select(newest);
   else this.container.querySelectorAll('[data-result]').forEach(button=>button.classList.toggle('selected',button.dataset.result===this.preview.current?.url));
  }
 }
 select(item){if(!item)return;this.preview.show(item);this.container.querySelectorAll('[data-result]').forEach(button=>button.classList.toggle('selected',button.dataset.result===item.url));}
}
export async function uploadFiles(projectId,files,onProgress){
 for(let index=0;index<files.length;index++){
  const file=files[index];if(file.size>2048*1024**2)throw new Error('单个文件不能超过 2 GB。');
  await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',href(`api/projects/${projectId}/assets`));xhr.upload.onprogress=e=>onProgress?.({name:file.name,index:index+1,total:files.length,percent:e.lengthComputable?e.loaded/e.total*100:0});xhr.onerror=()=>reject(new Error('上传中断，请重试。'));xhr.onload=()=>{if(xhr.status>=200&&xhr.status<300)resolve();else{let data={detail:'上传失败'};try{data=JSON.parse(xhr.responseText);}catch{}reject(new Error(errorText(data,xhr.status,xhr.getResponseHeader('X-Request-ID'))));}};const form=new FormData();form.append('file',file);xhr.send(form);});
 }
}
export class AssetLibrary{
 constructor(container,project,{select,onChange,checkboxes=false,checked=new Set(),onChecked,attach}={}){this.container=container;this.project=project;this.options={select,onChange,checkboxes,checked,onChecked,attach};this.signature='';container.innerHTML=`<div class="panel-heading"><h3>素材库 <span class="asset-count"></span></h3><button class="icon-button asset-upload" title="上传素材">${icon('plus')}</button></div><label class="dropzone">${icon('upload',24)}<strong>上传音视频或图片</strong><span>点击选择或拖入文件</span><small>视频 / 音频 / PNG / JPG / WebP / SRT · 每个文件最多 2 GB</small><input type="file" multiple accept="video/*,audio/*,image/*,.mkv,.ogv,.srt,.m4a,.flac,.opus" hidden></label><div class="upload-progress" hidden><div><span></span><b></b></div><progress max="100"></progress></div><div class="asset-list"></div>`;
  this.input=container.querySelector('input');container.querySelector('.asset-upload').onclick=()=>this.input.click();this.input.onchange=()=>this.upload([...this.input.files]);const drop=container.querySelector('.dropzone');drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragging');};drop.ondragleave=()=>drop.classList.remove('dragging');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragging');this.upload([...e.dataTransfer.files]);};this.update(project);}
 update(project){this.project=project;const signature=JSON.stringify(project.assets);if(signature===this.signature)return;this.signature=signature;const items=sourceItems(project);this.container.querySelector('.asset-count').textContent=items.length;
  const list=this.container.querySelector('.asset-list');list.innerHTML=items.length?items.map(item=>`<article class="asset-entry"><button class="asset-cover" data-source="${item.id}" aria-label="预览 ${esc(item.name)}">${item.poster_url?`<img loading="lazy" src="${href(item.poster_url)}" alt="${esc(item.name)}">`:icon('file',28)}<span>${icon('play',16)}</span>${item.duration?`<small>${duration(item.duration)}</small>`:''}</button><div class="asset-entry-name">${this.options.checkboxes?`<input type="checkbox" data-check="${item.id}" ${this.options.checked.has(item.id)?'checked':''} ${item.kind!=='video'?'disabled':''} aria-label="选择 ${esc(item.name)}">`:''}<strong title="${esc(item.name)}">${esc(item.name)}</strong>${this.options.attach?`<button class="icon-button" data-attach="${item.id}" title="添加到当前消息">${icon('attach',14)}</button>`:''}</div><small>${esc(item.subtitle)}</small>${item.transcribed_tracks?.length?'<span class="cache-label">✓ 转写已就绪</span>':''}</article>`).join(''):'<div class="empty-small">还没有素材<br>可上传文件，或返回首页载入示例</div>';
  list.querySelectorAll('[data-source]').forEach(button=>button.onclick=()=>{const item=items.find(a=>a.id===button.dataset.source);(this.options.select||openMedia)(item);});list.querySelectorAll('[data-check]').forEach(box=>box.onchange=()=>{box.checked?this.options.checked.add(box.dataset.check):this.options.checked.delete(box.dataset.check);this.options.onChecked?.();});list.querySelectorAll('[data-attach]').forEach(button=>button.onclick=()=>this.options.attach(items.find(a=>a.id===button.dataset.attach)));
 }
 async upload(files){if(!files.length)return;const progress=this.container.querySelector('.upload-progress');progress.hidden=false;this.input.disabled=true;try{await uploadFiles(this.project.id,files,p=>{progress.querySelector('span').textContent=p.name;progress.querySelector('b').textContent=Math.round(p.percent)+'%';progress.querySelector('progress').value=p.percent;});const project=await api(`api/projects/${this.project.id}`);this.update(project);await this.options.onChange?.(project);toast('素材已上传');}catch(error){toast(error.message,true);}finally{progress.hidden=true;this.input.disabled=false;this.input.value='';}}
}
export function emptyError(error){document.querySelector('main').innerHTML=`<div class="page-error">${icon('file',40)}<h2>无法打开这个项目</h2><p>${esc(error.message)}</p><a class="button primary" href="${href('')}">返回示例首页</a></div>`;}
export async function removeProject(id){if(!confirm('删除此项目及其素材、对话和生成结果？'))return;await api(`api/projects/${id}`,{method:'DELETE'});location.assign(href(''));}
