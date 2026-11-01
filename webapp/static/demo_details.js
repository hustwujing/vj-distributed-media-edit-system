import {esc,href} from './common.js';

// Declare each demo deliberately. Build directories also contain internal artifacts,
// so their file lists must never determine the visible inputs or controls.
const DETAILS={
 'agent-portrait':{view:'flow',inputs:true,output:'竖屏成片'},
 transcribe:{view:'document',input:'待转写视频',output:'逐词文字稿'},
 batch:{view:'document',inputs:true,output:'整理后的素材笔记'},
 cuts:{view:'compare-video',before:'原始对白',after:'去除停顿后的成片'},
 grade:{view:'compare-video',before:'原始画面',after:'暖调成片'},
 subtitles:{view:'subtitle',subtitle:'master.srt'},
 overlay:{view:'flow',inputs:true,output:'画中画成片'},
 loudness:{view:'audio',inputs:true,output:'响度标准化结果'},
 timeline:{view:'single',output:'画面、波形与逐词时间线'},
 analysis:{view:'document',input:'待分析视频',output:'曝光测量报告'},
 multitrack:{view:'document',input:'双音轨视频',output:'人声音轨文字稿'},
 'local-speed':{view:'compare-video',before:'原始速度',after:'变速与定格结果'},
 'local-transition':{view:'compare-video',before:'原始素材',after:'叠化成片'},
 'local-reverse':{view:'compare-video',before:'原始动作',after:'倒放循环结果'},
 'local-geometry':{view:'compare-video',before:'原始横屏',after:'裁剪、旋转后的竖屏'},
 'local-split':{view:'compare-video',before:'原始画面',after:'左右分屏结果'},
 'local-mask':{view:'compare-video',before:'原始画面',after:'指定时段遮挡结果'},
 'local-music':{view:'audio',inputs:true,output:'对白与自动压低配乐'},
 'local-audio-replace':{view:'audio',inputs:true,output:'替换音轨后的成片'},
 'local-audio-clean':{view:'audio',input:'含底噪的对白',output:'降噪 WAV'},
 'local-word-edit':{view:'subtitle',subtitle:'final.srt'},
 'image-cutout':{view:'compare-image',before:'原图',after:'透明前景'},
 'image-select':{view:'compare-image',before:'原图与框选范围',after:'主体蒙版'},
 'image-local':{view:'compare-image',before:'原图',after:'仅修改杯子颜色'},
 'image-layers':{view:'layers'},
 'image-erase':{view:'compare-image',before:'含标题原图',after:'移除文字并补全背景'},
 'image-sam3-zim':{view:'stages'},
 'image-mask-refine':{view:'compare-image',before:'待清理蒙版',after:'精修结果'},
 'image-auto-adjust':{view:'compare-image',before:'调整前',after:'白平衡、色阶与曲线结果'},
 'image-color-match':{view:'reference',reference:'reference.png'},
 'image-product-layout':{view:'product',subject:'before.png'},
 'image-watermark-batch':{view:'compare-image',before:'批处理输入样例',after:'统一尺寸与水印结果'},
 'image-ocr':{view:'ocr',report:'ocr.json'},
 'image-replace-text':{view:'compare-image',before:'原始文字',after:'替换后的文字'},
 'image-perspective':{view:'compare-image',before:'倾斜文档',after:'四点校正结果'},
 'image-adjustment-layers':{view:'canvas',layers:['暖调调整图层','原始图片']},
 'image-text-effects':{view:'canvas',layers:['可编辑标题','原始图片']},
 'image-layout-guides':{view:'canvas',layers:['杯子 1','杯子 2','杯子 3']},
 'video-version-compare':{view:'compare-video',before:'版本 1',after:'版本 2'},
 'video-caption-refine':{view:'single',output:'字幕精修后的成片'},
 'video-chapter-shorts':{view:'single',output:'章节拆出的独立短片'},
 'video-brand-batch':{view:'single',output:'应用品牌规则后的成片'},
 'video-shot-notes':{view:'single',output:'按镜头批注返工后的版本'},
 'video-quality-review':{view:'quality',input:'待质检成片'}
};

const image=/\.(png|jpe?g|webp)$/i,video=/\.(mp4|mov|webm)$/i,audio=/\.(wav|mp3|m4a|aac|flac|ogg|opus)$/i;
const fileType=name=>image.test(name)?'image':video.test(name)?'video':audio.test(name)?'audio':'document';
const media=(url,type,poster='',label='')=>type==='image'?`<img src="${href(url)}" alt="${esc(label)}">`:type==='video'?`<video controls playsinline preload="metadata" ${poster?`poster="${href(poster)}"`:''} src="${href(url)}"></video>`:type==='audio'?`<audio controls preload="metadata" src="${href(url)}"></audio>`:`<pre data-document="${href(url)}">正在读取内容…</pre>`;
const sourceUrl=(d,s)=>`api/demos/${d.id}/sources/${encodeURIComponent(s.key)}`;
const fileUrl=(d,n)=>`api/demos/${d.id}/files/${encodeURIComponent(n)}`;
const before=d=>({url:fileUrl(d,d.before),type:d.before_type?.split('/')[0]||fileType(d.before)});
const result=d=>({url:fileUrl(d,d.result),type:d.result_type?.split('/')[0]||fileType(d.result)});
const panel=(label,content)=>`<article class="showcase-panel"><b>${esc(label)}</b><div>${content}</div></article>`;
const sourceCards=d=>`<div class="demo-input-grid">${d.sources.map(s=>`<article>${media(sourceUrl(d,s),fileType(s.file),'',s.name)}<small>${esc(s.name)}</small></article>`).join('')}</div>`;

function compare(d,detail,type){const a=before(d),b=result(d),poster=fileUrl(d,d.poster);return `<div class="demo-special compare ${type}"><div class="compare-labels"><span>${esc(detail.before)}</span><span>${esc(detail.after)}</span></div><div class="compare-panes">${media(a.url,a.type,'',detail.before)}${media(b.url,b.type,poster,detail.after)}</div>${type==='compare-video'?'<label class="sync-control">同步定位 <input data-compare-range type="range" min="0" max="100" value="0"></label>':''}</div>`;}
function flow(d,detail){return `<div class="demo-special source-flow">${sourceCards(d)}<div class="flow-arrow">处理后</div>${panel(detail.output,media(result(d).url,result(d).type,fileUrl(d,d.poster),detail.output))}</div>`;}
function documentView(d,detail){const input=detail.inputs?sourceCards(d):panel(detail.input,media(sourceUrl(d,d.sources[0]),fileType(d.sources[0].file),'',detail.input));return `<div class="demo-special document-view">${input}${panel(detail.output,media(result(d).url,result(d).type,'',detail.output))}</div>`;}
function audioView(d,detail){const input=detail.inputs?sourceCards(d):panel(detail.input,media(sourceUrl(d,d.sources[0]),fileType(d.sources[0].file),'',detail.input));return `<div class="demo-special audio-view">${input}${panel(detail.output,media(result(d).url,result(d).type,fileUrl(d,d.poster),detail.output))}</div>`;}
function subtitleView(d,detail){return `<div class="demo-special subtitle-view">${panel('字幕成片',media(result(d).url,result(d).type,fileUrl(d,d.poster),'字幕成片'))}${panel('实际字幕文件',media(fileUrl(d,detail.subtitle),'document'))}</div>`;}
function layersView(d){const layers=d.files.filter(n=>/^layer-\d+\.(png|webp)$/i.test(n));return `<div class="demo-special layers-view">${panel('合成预览',media(result(d).url,result(d).type,'','合成预览'))}<div class="layer-stack">${layers.map((n,i)=>`<div><span>${i+1}</span><b>透明图层 ${i+1}</b><small>${esc(n)}</small></div>`).join('')}<div><b>可编辑工程文件</b><small>PSD、ORA</small></div></div></div>`;}
function stagesView(d){const stages=(d.detail?.stages||[]).filter(s=>d.files.includes(s.file));return `<div class="demo-special mask-pipeline"><div class="mask-stage">${media(fileUrl(d,stages[0].file),fileType(stages[0].file),'',stages[0].label)}</div><div class="mask-stage-tabs" role="tablist" aria-label="SAM3 与 ZIM 实际处理阶段">${stages.map((s,i)=>`<button role="tab" aria-selected="${i?'false':'true'}" data-stage-file="${esc(s.file)}" class="${i?'':'selected'}"><span>${i+1}</span>${esc(s.label)}</button>`).join('')}</div></div>`;}
function referenceView(d,detail){const a=before(d),b=result(d);return `<div class="demo-special reference-view three-up">${panel('待匹配图片',media(a.url,a.type,'','待匹配图片'))}${panel('色彩参考图',media(fileUrl(d,detail.reference),'image','','色彩参考图'))}${panel('匹配结果',media(b.url,b.type,'','匹配结果'))}</div>`;}
function productView(d,detail){return `<div class="demo-special reference-view">${panel('透明主体',media(fileUrl(d,detail.subject),'image','','透明主体'))}<div class="flow-arrow">排版与投影 →</div>${panel('商品图结果',media(result(d).url,result(d).type,'','商品图结果'))}</div>`;}
function canvasView(d,detail){return `<div class="demo-special canvas-view">${panel('画布结果',media(result(d).url,'image','','画布结果'))}<div class="mini-layers">${detail.layers.map(x=>`<span>${esc(x)}</span>`).join('')}</div></div>`;}
function ocrView(d,detail){return `<div class="demo-special ocr-view">${panel('文字框预览',media(result(d).url,'image','','文字框预览'))}<div class="report-panel" data-json-lines="${href(fileUrl(d,detail.report))}"><div class="loading-state">正在读取识别结果…</div></div></div>`;}
function qualityView(d,detail){return `<div class="demo-special report-view">${panel(detail.input,media(sourceUrl(d,d.sources[0]),fileType(d.sources[0].file),'',detail.input))}<div class="report-panel quality" data-quality-url="${href(result(d).url)}"><div class="loading-state">正在读取质检结果…</div></div></div>`;}
function singleView(d,detail){return `<div class="demo-special single-view">${panel(detail.output,media(result(d).url,result(d).type,fileUrl(d,d.poster),detail.output))}</div>`;}

function body(d,detail){if(detail.view==='compare-image'||detail.view==='compare-video')return compare(d,detail,detail.view);if(detail.view==='flow')return flow(d,detail);if(detail.view==='document')return documentView(d,detail);if(detail.view==='audio')return audioView(d,detail);if(detail.view==='subtitle')return subtitleView(d,detail);if(detail.view==='layers')return layersView(d);if(detail.view==='stages')return stagesView(d);if(detail.view==='reference')return referenceView(d,detail);if(detail.view==='product')return productView(d,detail);if(detail.view==='canvas')return canvasView(d,detail);if(detail.view==='ocr')return ocrView(d,detail);if(detail.view==='quality')return qualityView(d,detail);return singleView(d,detail);}

const internal=/^(before\.|poster\.|reference\.png$|transcript_.*\.json$|plan\.json$|edit_plan\.json$|edl\.json$)|\.report\.json$|\.verify_\d+\.jpg$|layer-report\.json$|sam3-preview\.jpg$|zim-preview\.jpg$|sam3-zim\.json$/i;
const deliverables=d=>d.files.filter(n=>!internal.test(n));
const genericOption=/^(?:原图保留，结果可|可在图片工作台手动框选和调整图层|可载入项目继续编辑)/;
const options=d=>`<section class="demo-prompt"><h3>可以这样描述</h3><blockquote>${esc(d.prompt)}</blockquote></section><section class="demo-parameters"><h3>本次操作</h3>${d.options.filter(x=>!genericOption.test(x)).map(x=>`<p>${esc(x)}</p>`).join('')}</section>`;

export function renderDemoDetail(d){const detail=DETAILS[d.id];if(!detail)throw Error(`示例 ${d.id} 缺少详情配置`);return {kind:detail.view,html:`<div class="demo-capability-layout" data-demo-id="${esc(d.id)}"><section class="demo-stage"><div class="capability-badge">${esc(d.category)}</div>${body(d,detail)}</section><aside>${options(d)}<section class="demo-deliverables"><h3>可用结果</h3>${deliverables(d).map(n=>`<div><span class="file-dot ${fileType(n)}"></span><b>${esc(n)}</b></div>`).join('')}</section></aside></div>`,bind(root){
 root.querySelectorAll('[data-stage-file]').forEach(button=>button.onclick=()=>{root.querySelectorAll('[data-stage-file]').forEach(x=>{x.classList.toggle('selected',x===button);x.setAttribute('aria-selected',x===button?'true':'false');});const name=button.dataset.stageFile;root.querySelector('.mask-stage').innerHTML=media(fileUrl(d,name),fileType(name),'',button.textContent.trim());});
 const range=root.querySelector('[data-compare-range]'),videos=[...root.querySelectorAll('.compare-panes video')];if(range&&videos.length===2)range.oninput=()=>{const finite=videos.map(v=>v.duration).filter(Number.isFinite);if(!finite.length)return;const t=Number(range.value)/100*Math.min(...finite);videos.forEach(v=>v.currentTime=t);};
 root.querySelectorAll('[data-document]').forEach(async pre=>{try{const response=await fetch(pre.dataset.document);if(!response.ok)throw Error();pre.textContent=(await response.text()).slice(0,10000);}catch{pre.textContent='内容暂不可读';}});
 root.querySelectorAll('[data-quality-url]').forEach(async panel=>{try{const response=await fetch(panel.dataset.qualityUrl),data=await response.json(),checks=data.checks||[];panel.innerHTML=`<h3>${esc(data.summary||'质检报告')}</h3>${checks.map(c=>`<div class="quality-row ${esc(c.status)}"><b>${esc(c.name)}</b><span>${esc(c.detail||c.status)}</span></div>`).join('')}<small>机器信号需结合播放器复核</small>`;}catch{panel.innerHTML='<p>报告暂不可读</p>';}});
 root.querySelectorAll('[data-json-lines]').forEach(async panel=>{try{const response=await fetch(panel.dataset.jsonLines),data=await response.json(),lines=data.lines||[];panel.innerHTML=`<h3>识别到 ${lines.length} 处文字</h3>${lines.map(line=>`<div class="ocr-result"><b>${esc(line.text||'')}</b><span>位置 ${esc((line.box||[]).join(', '))}</span><small>置信度 ${Math.round(Number(line.confidence||0)*100)}%</small></div>`).join('')}`;}catch{panel.innerHTML='<p>识别结果暂不可读</p>';}});
 }};}

export const configuredDemoIds=Object.freeze(Object.keys(DETAILS));
