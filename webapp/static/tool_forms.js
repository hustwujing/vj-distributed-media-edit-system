import {$,esc,icon,duration} from './common.js';

export const TOOL_GROUPS={video:'画面剪辑',audio:'声音处理',text:'文字字幕',inspect:'素材分析'};
export const TOOLS={
 render:{name:'剪切与拼接',group:'video',description:'选择片段并排列播放顺序，可在高级选项中调色、加字幕或叠加视频。',submit:'生成视频',multi:true},
 speed:{name:'视频变速',group:'video',description:'改变选中片段的速度，同时保持原声音调。',submit:'生成变速视频'},
 motion:{name:'倒放、定格与循环',group:'video',description:'倒放画面、在末尾停留，或重复播放选中片段。',submit:'生成视频'},
 geometry:{name:'裁剪、旋转与画幅',group:'video',description:'调整可见区域、方向和画幅；输出尺寸控制最终画布。',submit:'生成视频'},
 transition:{name:'片段转场',group:'video',description:'至少两段视频，在相邻片段之间加入画面与声音的交叠过渡。',submit:'生成转场视频',multi:true},
 split:{name:'分屏合成',group:'video',description:'左右、上下或四宫格同时播放；以最短视图结束，保留指定视图的声音。',submit:'生成分屏视频',multi:true},
 mask:{name:'局部打码与遮挡',group:'video',description:'在最终画布的固定矩形内打码，不自动跟随移动目标。',submit:'生成打码视频'},
 grade:{name:'画面调色',group:'video',description:'调整选中片段的曝光与色彩风格。',submit:'生成调色视频'},
 mix:{name:'配乐与声音替换',group:'audio',description:'保留原声并配乐，或换成新的音轨；可控制音乐开始时间和自动压低。',submit:'生成配乐视频'},
 audio:{name:'音频提取与清理',group:'audio',description:'从视频或音频中截取声音，调整音量、减轻持续底噪并导出。',submit:'生成音频',audioOnly:true},
 titles:{name:'文字标题',group:'text',description:'在视频上添加中文标题，单独控制样式、位置和显示时间。',submit:'生成标题视频'},
 subtitles:{name:'字幕样式与高亮',group:'text',description:'使用已有逐词转写或上传的 SRT；这里不会自动发起付费转写。',submit:'生成字幕视频'},
 transcribe:{name:'语音转写',group:'text',description:'勾选要识别的素材。新转写使用 ElevenLabs 额度，已有缓存直接复用。',submit:'开始转写'},
 pack:{name:'整理素材笔记',group:'text',description:'整理本项目已有转写，根据说话人和停顿分段，不发起新识别。',submit:'生成素材笔记'},
 timeline:{name:'画面与声音时间线',group:'inspect',description:'生成指定范围的画面采样、声音波形与词标签预览图。',submit:'生成时间线预览'},
 analyze:{name:'曝光与色彩分析',group:'inspect',description:'分析亮度、对比度、饱和度并给出校正建议，不生成视频。',submit:'分析画面'},
 review:{name:'成片质检',group:'inspect',description:'检测黑帧、长静帧、长静音、时长和音视频轨道。结果是确定性信号，需结合预览判断，不使用额外模型。',submit:'开始质检'},
};
const legacy=new Set(['render','grade','transcribe','pack','timeline','analyze','review']);
const numerical=new Set(['start','end','audio_track','speed','freeze_end','repeat','rotate','crop_x','crop_y','crop_w','crop_h','width','height','fps','crf','transition_duration','split_audio','mask_x','mask_y','mask_w','mask_h','effect_start','effect_end','size','outline','words_per_line','music_start','music_end','offset','volume','highpass','fade_in','fade_out','silence','frames','workers','speakers']);
const options=(items,value)=>items.map(([v,t])=>`<option value="${esc(v)}" ${String(v)===String(value)?'selected':''}>${esc(t)}</option>`).join('');
const grades=[['none','保持原色'],['auto','自动校正'],['subtle','轻微增强'],['neutral_punch','自然通透'],['warm_cinematic','暖调电影感']];
const positions=[['top','顶部'],['center','居中'],['bottom','底部']];

export class ToolForm{
 constructor(project,preset,onChange){
  this.project=project;this.onChange=onChange;this.key='updream:tool-drafts:'+project.id;this.states={};
  try{const saved=JSON.parse(localStorage.getItem(this.key)||'{}');if(saved&&typeof saved==='object'&&!Array.isArray(saved))this.states=saved;}catch{}
  this.active=TOOLS[preset?.tool]?preset.tool:preset?.kind==='render'&&preset.grade&&preset.grade!=='none'?'grade':TOOLS[preset?.kind]?preset.kind:'render';
  if(preset)this.states[this.active]=this.fromPreset(preset,this.states[this.active]||{});
  this.form=$('tool-settings');
  $('tool-select').onchange=()=>this.select($('tool-select').value);
  this.render();
 }
 get info(){return TOOLS[this.active];}
 get media(){return this.project.assets.filter(a=>['video','audio'].includes(a.kind));}
 get videos(){return this.media.filter(a=>a.kind==='video');}
 get state(){
  const pool=this.info.audioOnly?this.media:this.videos,a=pool[0];
  const initial={asset:a?.id||'',start:0,end:a?Math.min(10,a.duration):0,audio_track:0,
   speed:2,reverse:false,reverse_audio:false,freeze_end:0,repeat:1,crop:false,crop_x:0,crop_y:0,crop_w:a?.width||640,crop_h:a?.height||360,
   rotate:0,flip:'none',fit:'contain',sizePreset:'source',width:a?.width||1280,height:a?.height||720,fps:30,crf:20,
   transition:'fade',transition_duration:.4,layout:'horizontal',split_audio:0,
   mask_x:0,mask_y:0,mask_w:160,mask_h:80,mask_mode:'pixelate',full:true,effect_start:0,effect_end:a?Math.min(10,a.duration):0,
   text:'',size:36,color:'#FFFFFF',background:true,position:'bottom',sub_source:'transcript',subtitle_asset:'',words_per_line:4,highlight:false,highlight_color:'#FFE066',outline:2,
   music_asset:'',music_start:0,music_end:0,mode:'mix',offset:0,volume:.2,loop:true,duck:true,
   format:'wav',clean_volume:1,denoise:false,normalize:true,highpass:0,fade_in:.1,fade_out:.1,grade:'none',quality:'draft',normalize_audio:true,
   language:'',speakers:'',workers:2,silence:.5,frames:6,assets:[],segments:[],overlays:[],legacy_subtitles:'none',legacy_fps:''};
  return this.states[this.active] ||= initial;
 }
 fromPreset(p,old){
  // Only restore fields represented by this tool; server defaults are not other tools' state.
  const s={...old};
  if(p.kind==='edit'&&p.edit_plan){
   const plan=p.edit_plan,c=plan.clips?.[0]||{},o=plan.output||{},a=plan.audio||{};
   Object.assign(s,{asset:c.asset,start:c.start??0,end:c.end??0,audio_track:c.audio_track??0,segments:plan.clips||[],...o});
   if(o.width&&o.height)s.sizePreset='custom';
   for(const key of ['speed','reverse','reverse_audio','freeze_end','repeat','rotate','flip','fit'])if(key in c)s[key]=c[key];
   if(c.crop)Object.assign(s,{crop:true,crop_x:c.crop[0],crop_y:c.crop[1],crop_w:c.crop[2],crop_h:c.crop[3]});
   if(plan.transition)Object.assign(s,{transition:plan.transition.type,transition_duration:plan.transition.duration});
   if(plan.layout)s.layout=plan.layout;if(plan.split_audio!==undefined)s.split_audio=plan.split_audio;
   if(plan.masks?.[0]){const m=plan.masks[0];Object.assign(s,{mask_x:m.x,mask_y:m.y,mask_w:m.width,mask_h:m.height,mask_mode:m.mode,full:m.start===undefined&&m.end===undefined,effect_start:m.start||0,effect_end:m.end||c.end-c.start});}
   if(plan.titles?.[0]){const t=plan.titles[0];Object.assign(s,t,{full:t.start===undefined&&t.end===undefined,effect_start:t.start||0,effect_end:t.end||c.end-c.start});s.start=c.start??0;s.end=c.end??0;}
   if(plan.subtitles){const t=plan.subtitles;Object.assign(s,{sub_source:t.asset?'upload':'transcript',subtitle_asset:t.asset||'',words_per_line:t.words_per_line||4,size:t.size||36,color:t.color||'#FFFFFF',position:t.position||'bottom',outline:t.outline??2,highlight:!!t.highlight,highlight_color:t.highlight||'#FFE066'});}
   if(this.active==='audio')Object.assign(s,{format:o.format||'wav',clean_volume:a.volume??1,denoise:!!a.denoise,normalize:a.normalize!==false,highpass:a.highpass||0,fade_in:a.fade_in??.1,fade_out:a.fade_out??.1});
   if(this.active==='mix')Object.assign(s,{music_asset:a.asset||'',music_start:a.start||0,music_end:a.end||0,mode:a.mode||'mix',offset:a.offset||0,volume:a.volume??.2,loop:!!a.loop,duck:!!a.duck,fade_in:a.fade_in??.1,fade_out:a.fade_out??.1});
  }else{
   for(const key of ['language','speakers','audio_track','workers','silence','frames','grade','quality','normalize_audio','assets','segments','overlays'])if(key in p)s[key]=p[key];
   if(p.assets?.[0])s.asset=p.assets[0];
   if(p.segments?.[0])Object.assign(s,{asset:p.segments[0].asset,start:p.segments[0].start,end:p.segments[0].end,audio_track:p.segments[0].audio_track||0});
   if(['timeline','analyze'].includes(p.kind))Object.assign(s,{start:p.start,end:p.end});
   s.legacy_subtitles=p.subtitles||'none';s.subtitle_asset=p.subtitle_asset||'';s.legacy_fps=p.fps||'';
  }
  return s;
 }
 ensure(){
  const current=this.states[this.active];delete this.states[this.active];const defaults=this.state;this.states[this.active]={...defaults,...current};
  const s=this.state,pool=this.info.audioOnly?this.media:this.videos;
  if(!pool.some(a=>a.id===s.asset)&&pool.length)this.changeSource(s,pool[0].id);
  if(this.info.multi&&!s.segments.length&&pool[0])s.segments=[this.newClip(pool[0])];
  if(this.active==='split'){const n=s.layout==='grid'?4:2;while(s.segments.length<n)s.segments.push(this.newClip(pool[s.segments.length%Math.max(1,pool.length)]));s.segments=s.segments.slice(0,n);s.split_audio=Math.min(Number(s.split_audio)||0,n-1);}
 }
 newClip(a){return {asset:a?.id||'',start:0,end:a?Math.min(10,a.duration):0,audio_track:0};}
 changeSource(s,id){const a=this.media.find(a=>a.id===id);s.asset=id;s.start=0;s.end=a?Math.min(10,a.duration):0;s.audio_track=0;s.crop_x=0;s.crop_y=0;s.crop_w=a?.width||640;s.crop_h=a?.height||360;}
 persist(){try{localStorage.setItem(this.key,JSON.stringify(this.states));}catch{}}
 select(id){this.persist();this.active=id;this.render();this.onChange();}
 updateProject(project){const changed=JSON.stringify(project.assets)!==JSON.stringify(this.project.assets);this.project=project;if(changed)this.render();}
 selectField(key,label,items,extra=''){return `<label class="field">${label}<select data-option="${key}" ${extra}>${options(items,this.state[key])}</select></label>`;}
 number(key,label,min=0,max=99999,step=.1){return `<label class="field">${label}<input data-option="${key}" type="number" min="${min}" max="${max}" step="${step}" value="${esc(this.state[key]??'')}" required></label>`;}
 check(key,label){return `<label class="checkbox-field"><input type="checkbox" data-option="${key}" ${this.state[key]?'checked':''}>${label}</label>`;}
 color(key,label){return `<label class="field">${label}<input type="color" data-option="${key}" value="${this.state[key]}"></label>`;}
 section(title,body,hint=''){return `<section class="parameter-section"><h3>${title}</h3>${hint?`<p class="form-hint">${hint}</p>`:''}${body}</section>`;}
 grid(...fields){return '<div class="field-grid">'+fields.join('')+'</div>';}
 tracks(asset){const a=this.media.find(a=>a.id===asset);return a?.audio_tracks?.length?a.audio_tracks.map((t,i)=>[i,t.title||`音轨 ${i+1}`]):[[0,'无原声（静音）']];}
 range(){const s=this.state,a=this.media.find(a=>a.id===s.asset),pool=this.info.audioOnly?this.media:this.videos;return this.section('1 · 选择素材与片段',
  this.selectField('asset',this.info.audioOnly?'音视频素材':'视频素材',[['','请选择素材'],...pool.map(a=>[a.id,a.name])],'required')+
  this.grid(this.number('start','开始（秒）',0,a?.duration||99999,.01),this.number('end','结束（秒）',.01,a?.duration||99999,.01))+
  (['analyze','review'].includes(this.active)?'':this.selectField('audio_track','使用音轨',this.tracks(s.asset))),a?`素材时长 ${a.duration.toFixed(2)} 秒`:'先在左侧上传素材');}
 clips(){const s=this.state,split=this.active==='split';let body='';
  if(split)body+=this.selectField('layout','分屏布局',[['horizontal','左右两屏'],['vertical','上下两屏'],['grid','四宫格']]);
  body+=s.segments.map((c,i)=>`<div class="segment-card"><div class="segment-card-top"><span>${split?'视图':'片段'} ${i+1}</span>${split?'':`<div><button type="button" class="icon-button" data-move="${i}" title="上移">↑</button><button type="button" class="icon-button" data-remove="${i}" title="移除">×</button></div>`}</div><select data-clip="${i}" data-key="asset" aria-label="片段 ${i+1} 素材" required>${options([['','请选择素材'],...this.videos.map(a=>[a.id,a.name])],c.asset)}</select><div class="segment-times"><label>开始<input type="number" data-clip="${i}" data-key="start" min="0" step=".01" value="${c.start}" required></label><label>结束<input type="number" data-clip="${i}" data-key="end" min=".01" step=".01" value="${c.end}" required></label><label>音轨<select data-clip="${i}" data-key="audio_track">${options(this.tracks(c.asset),c.audio_track||0)}</select></label></div></div>`).join('');
  if(!split)body+='<button type="button" class="button dashed wide" id="add-clip">＋ 添加片段</button>';
  else body+=this.selectField('split_audio','保留哪个视图的声音',s.segments.map((_,i)=>[i,`视图 ${i+1}`]));
  return this.section('1 · '+(split?'安排分屏素材':'安排片段顺序'),body,split?'不同画幅会按所选方式适配各自区域。':'按列表顺序播放，可重复选择同一素材的不同片段。');}
 output(){const s=this.state;
  return this.section('3 · 输出设置',this.selectField('sizePreset','输出画幅',[['source','跟随首个素材'],['landscape','横屏 1280 × 720'],['portrait','竖屏 720 × 1280'],['square','正方形 1080 × 1080'],['custom','自定义尺寸']])+
   (s.sizePreset==='custom'?this.grid(this.number('width','宽度（像素）',16,4096,2),this.number('height','高度（像素）',16,4096,2)):'')+
   this.selectField('fit','素材如何填充画布',[['contain','完整保留，空白留黑'],['cover','居中裁满画布'],['blur','完整保留，模糊背景']])+
   `<details class="advanced-options"><summary>高级输出选项</summary>${this.grid(this.selectField('fps','帧率',[[24,'24 fps'],[25,'25 fps'],[30,'30 fps'],[50,'50 fps'],[60,'60 fps']]),this.selectField('crf','质量',[[18,'高质量'],[20,'标准'],[26,'快速预览']]))}</details>`);
 }
 timed(){return this.check('full','全程显示')+(this.state.full?'':this.grid(this.number('effect_start','成片开始（秒）',0,3600,.01),this.number('effect_end','成片结束（秒）',.01,3600,.01)));}
 settings(){const s=this.state;let body='';
  switch(this.active){
   case 'speed':body=this.selectField('speed','播放速度',[[.25,'0.25 倍'],[.5,'0.5 倍'],[.75,'0.75 倍'],[1,'原速'],[1.25,'1.25 倍'],[1.5,'1.5 倍'],[2,'2 倍'],[3,'3 倍'],[4,'4 倍']])+'<p class="form-hint">原声音调自动保持；慢放使用原有帧，不生成新画面。</p>';break;
   case 'motion':body=this.check('reverse','倒放画面')+(s.reverse?this.check('reverse_audio','同时倒放声音（否则静音）'):'')+this.grid(this.number('freeze_end','片尾定格（秒）',0,30,.1),this.number('repeat','循环次数',1,20,1));break;
   case 'geometry':body=this.grid(this.selectField('rotate','顺时针旋转',[[0,'不旋转'],[90,'90°'],[180,'180°'],[270,'270°']]),this.selectField('flip','镜像',[['none','不镜像'],['horizontal','水平'],['vertical','垂直']]))+this.check('crop','裁剪源画面区域')+(s.crop?this.grid(this.number('crop_x','左边距 X',0,4096,2),this.number('crop_y','上边距 Y',0,4096,2),this.number('crop_w','裁剪宽度',2,4096,2),this.number('crop_h','裁剪高度',2,4096,2))+'<p class="form-hint">使用原素材显示方向的像素坐标，先裁剪，再旋转。</p>':'');break;
   case 'transition':body=this.grid(this.selectField('transition','转场效果',[['fade','交叉淡化'],['pixel_dissolve','随机像素溶解'],['wipeleft','向左擦除'],['wiperight','向右擦除'],['slideleft','向左滑动'],['slideright','向右滑动'],['fadeblack','经黑场过渡']]),this.number('transition_duration','重叠时长（秒）',.05,3,.05))+'<p class="form-hint">重叠时间会缩短总片长，不能超过最短片段的一半。</p>';break;
   case 'split':body='<p class="form-hint">视图按从左到右、从上到下排列。下方填充方式分别作用于每个视图。</p>';break;
   case 'mask':body=this.selectField('mask_mode','遮挡方式',[['pixelate','马赛克'],['blur','模糊'],['black','黑色遮挡']])+this.grid(this.number('mask_x','画布左边距 X',0,4096,2),this.number('mask_y','画布上边距 Y',0,4096,2),this.number('mask_w','区域宽度',4,4096,2),this.number('mask_h','区域高度',4,4096,2))+this.timed()+'<p class="form-hint">坐标基于下方输出画布；移动目标请让矩形覆盖完整运动范围。</p>';break;
   case 'titles':body=`<label class="field">标题内容<textarea data-option="text" maxlength="500" rows="3" placeholder="输入标题，支持换行" required>${esc(s.text)}</textarea></label>`+this.grid(this.number('size','字号',10,200,1),this.selectField('position','位置',positions),this.color('color','文字颜色'))+this.check('background','添加半透明背景')+this.timed();break;
   case 'subtitles':body=this.selectField('sub_source','字幕来源',[['transcript','本项目已有逐词转写'],['upload','上传的 SRT']])+(s.sub_source==='upload'?this.selectField('subtitle_asset','SRT 文件',[['','请选择 SRT'],...this.project.assets.filter(a=>a.kind==='subtitle').map(a=>[a.id,a.name])],'required')+'<p class="form-hint">SRT 时间须对应本次成片；可上传已编辑的双语字幕。</p>':this.number('words_per_line','每行最多词数',1,20,1)+this.check('highlight','逐词高亮')+(s.highlight?this.color('highlight_color','高亮颜色'):''))+this.grid(this.number('size','字号',10,200,1),this.selectField('position','字幕位置',positions),this.color('color','字幕颜色'),this.number('outline','描边宽度',0,8,1));break;
   case 'mix':{const m=this.media.find(a=>a.id===s.music_asset);body=this.selectField('music_asset','配乐 / 新音轨',[['','请选择含声音的素材'],...this.media.filter(a=>a.audio_tracks?.length).map(a=>[a.id,a.name])],'required')+this.selectField('mode','处理方式',[['mix','与原声混合'],['replace','替换原声']])+this.grid(this.number('music_start','音频开始（秒）',0,m?.duration||99999,.01),this.number('music_end','音频结束（秒）',.01,m?.duration||99999,.01),this.number('offset','在成片延迟（秒）',0,3600,.01),this.number('volume','新音轨音量倍数',0,4,.05))+this.check('loop','循环补足到成片结束')+(s.mode==='mix'?this.check('duck','原声响起时自动压低配乐'):'')+this.grid(this.number('fade_in','开头淡入（秒）',.03,30,.01),this.number('fade_out','结尾淡出（秒）',.03,30,.01));break;}
   case 'audio':body=this.grid(this.selectField('format','音频格式',[['wav','WAV · 无损 PCM'],['mp3','MP3'],['m4a','M4A']]),this.number('clean_volume','音量倍数',0,4,.05))+this.check('normalize','统一到 -16 LUFS（播客 / 视频）')+this.check('denoise','减轻持续底噪')+`<details class="advanced-options"><summary>声音修饰与淡化</summary>${this.number('highpass','高通截止频率（Hz，0 关闭）',0,300,10)}${this.grid(this.number('fade_in','开头淡入（秒）',.03,30,.01),this.number('fade_out','结尾淡出（秒）',.03,30,.01))}</details>`;break;
   case 'grade':body=this.selectField('grade','色彩处理',grades)+this.selectField('quality','输出质量',[['draft','720p 草稿'],['preview','1080p 预览'],['final','1080p 成片']]);break;
   case 'timeline':body=this.number('frames','采样画面数量',2,16,1);break;
   case 'analyze':body='<p class="form-hint">只输出测量数据与校正建议，不改变素材。</p>';break;
   case 'review':body='<p class="form-hint">检查黑帧、长静帧、长静音、音视频轨与时长。不改动素材；报告中的信号应在预览中复核。</p>';break;
  }
  return this.section('2 · '+this.info.name,body);
 }
 render(){
  const opened=this.renderedTool===this.active?[...this.form.querySelectorAll('details[open]')].map(d=>d.querySelector('summary')?.textContent):[];
  this.ensure();const s=this.state,t=this.active,info=this.info;this.renderedTool=t;
  $('tool-groups').innerHTML=Object.entries(TOOL_GROUPS).map(([id,name])=>`<button type="button" data-group="${id}" class="${id===info.group?'selected':''}">${name}</button>`).join('');
  $('tool-groups').querySelectorAll('button').forEach(b=>b.onclick=()=>this.select(Object.keys(TOOLS).find(k=>TOOLS[k].group===b.dataset.group)));
  $('tool-select').innerHTML=options(Object.entries(TOOLS).filter(([,v])=>v.group===info.group).map(([id,v])=>[id,v.name]),t);
  $('tool-description').textContent=info.description;$('tool-run').textContent=info.submit;$('tool-error').hidden=true;
  if(t==='pack')this.form.innerHTML=this.section('已有转写 → 素材笔记',this.number('silence','分段停顿（秒）',.1,5,.1),'按说话人变化和超过此长度的停顿分段，读取本项目全部已缓存转写。');
  else if(t==='transcribe')this.form.innerHTML=this.section('1 · 选择要转写的素材',this.media.filter(a=>a.audio_tracks?.length).map(a=>`<label class="checkbox-field"><input type="checkbox" data-batch="${a.id}" ${s.assets.includes(a.id)?'checked':''}>${esc(a.name)}</label>`).join('')||'<p class="empty-small">请先上传含声音的视频或音频。</p>')+this.section('2 · 识别参数',this.grid(this.selectField('language','语言',[['','自动识别'],['zh','中文'],['en','英语'],['ja','日语'],['ko','韩语'],['fr','法语'],['es','西班牙语']]),`<label class="field">说话人数（可选）<input type="number" data-option="speakers" min="1" max="32" value="${s.speakers??''}" placeholder="自动"></label>`)+`<details class="advanced-options"><summary>音轨与并发</summary>${this.grid(this.number('audio_track','音轨序号（从 0 开始）',0,15,1),this.selectField('workers','同时处理数量',[[1,'1 个'],[2,'2 个'],[4,'4 个']]))}</details>`);
  else if(t==='render')this.form.innerHTML=this.clips()+this.section('2 · 合成设置',this.selectField('quality','输出质量',[['draft','720p 草稿'],['preview','1080p 预览'],['final','1080p 成片']])+this.check('normalize_audio','统一响度')+`<details class="advanced-options"><summary>调色、字幕与视频叠加</summary>${this.selectField('grade','调色风格',grades)}${this.selectField('legacy_subtitles','字幕',[['none','不添加'],['transcript','使用已有转写'],['upload','上传的 SRT']])}${s.legacy_subtitles==='upload'?this.selectField('subtitle_asset','SRT 文件',[['','请选择'],...this.project.assets.filter(a=>a.kind==='subtitle').map(a=>[a.id,a.name])],'required'):''}<div id="legacy-overlays">${s.overlays.map((o,i)=>`<div class="overlay-card"><select data-overlay="${i}" data-key="asset">${options(this.videos.map(a=>[a.id,a.name]),o.asset)}</select><div class="segment-times"><label>出场<input type="number" data-overlay="${i}" data-key="start" value="${o.start}" min="0" step=".1" required></label><label>持续<input type="number" data-overlay="${i}" data-key="duration" value="${o.duration}" min=".1" step=".1" required></label><button type="button" data-remove-overlay="${i}" class="icon-button">×</button></div></div>`).join('')}</div><button id="add-overlay" type="button" class="button subtle wide">＋ 叠加视频</button><p class="form-hint">叠加显示在左上角，不混入叠加素材声音。</p></details>`);
  else this.form.innerHTML=(info.multi?this.clips():this.range())+this.settings()+(!legacy.has(t)&&!info.audioOnly?this.output():'');
  if(t==='render'||t==='grade'){
   const fields=this.selectField('legacy_fps','输出帧率',[['','跟随素材'],['24','24 fps'],['25','25 fps'],['30','30 fps'],['50','50 fps'],['60','60 fps'],['30000/1001','29.97 fps']]);
   (this.form.querySelector('.advanced-options')||this.form.lastElementChild).insertAdjacentHTML('beforeend',fields);
  }
  this.form.querySelectorAll('details').forEach(d=>{if(opened.includes(d.querySelector('summary')?.textContent))d.open=true;});
  this.bind();this.summary();
 }
 bind(){
  const rerender=new Set(['asset','crop','reverse','layout','sizePreset','full','mode','sub_source','highlight','legacy_subtitles','music_asset']);
  this.form.querySelectorAll('[data-option]').forEach(el=>el.addEventListener('change',()=>{
   const key=el.dataset.option,s=this.state;
   s[key]=el.type==='checkbox'?el.checked:(numerical.has(key)||key==='clean_volume')?(el.value===''?'':Number(el.value)):el.value;
   if(key==='asset')this.changeSource(s,el.value);
   if(key==='music_asset'){const a=this.media.find(a=>a.id===el.value);s.music_start=0;s.music_end=a?.duration||0;}
   if(key==='sub_source'&&s.sub_source==='upload')s.highlight=false;
   if(rerender.has(key))this.render();else this.summary();this.persist();this.onChange();
  }));
  this.form.querySelectorAll('[data-batch]').forEach(el=>el.onchange=()=>{this.state.assets=[...this.form.querySelectorAll('[data-batch]:checked')].map(e=>e.dataset.batch);this.summary();this.persist();this.onChange();});
  this.form.querySelectorAll('[data-clip],[data-overlay]').forEach(el=>el.onchange=()=>{
   const overlay='overlay' in el.dataset,index=+(overlay?el.dataset.overlay:el.dataset.clip),key=el.dataset.key;
   const c=(overlay?this.state.overlays:this.state.segments)[index];c[key]=key==='asset'?el.value:Number(el.value);
   if(key==='asset'){const a=this.videos.find(a=>a.id===el.value);if(overlay){c.start=0;c.duration=Math.min(3,a?.duration||0);}else Object.assign(c,this.newClip(a));this.render();}
   this.summary();this.persist();this.onChange();
  });
  this.form.querySelectorAll('[data-remove],[data-move],[data-remove-overlay]').forEach(b=>b.onclick=()=>{const s=this.state;if('remove' in b.dataset)s.segments.splice(+b.dataset.remove,1);else if('removeOverlay' in b.dataset)s.overlays.splice(+b.dataset.removeOverlay,1);else{const i=+b.dataset.move;if(i)[s.segments[i-1],s.segments[i]]=[s.segments[i],s.segments[i-1]];}this.render();this.persist();this.onChange();});
  $('add-clip')?.addEventListener('click',()=>{this.state.segments.push(this.newClip(this.videos[0]));this.render();this.persist();this.onChange();});
  $('add-overlay')?.addEventListener('click',()=>{if(!this.videos.length)return;this.state.overlays.push({asset:this.videos[0].id,start:0,duration:Math.min(3,this.videos[0].duration)});this.render();this.form.querySelector('.advanced-options').open=true;this.persist();this.onChange();});
 }
 dimensions(){const s=this.state,a=this.media.find(a=>a.id===(this.info.multi?s.segments[0]?.asset:s.asset));const unit=this.active==='split'?4:2;return {source:[Math.ceil((a?.width||1280)/unit)*unit,Math.ceil((a?.height||720)/unit)*unit],landscape:[1280,720],portrait:[720,1280],square:[1080,1080],custom:[s.width,s.height]}[s.sizePreset]||[1280,720];}
 summary(){const s=this.state,t=this.active;let text='';
  if(t==='transcribe')text=`已选 ${s.assets.length} 个素材 · 生成文字稿与逐词时间戳`;
  else if(t==='pack')text='读取当前项目已有转写 · 输出 Markdown';
  else if(['timeline','analyze','review'].includes(t))text=t==='timeline'?'输出图片，不修改视频':t==='review'?'输出质检报告，不修改视频':'输出分析报告，不修改视频';
  else{let seconds=this.info.multi?(t==='split'?Math.min(...s.segments.map(c=>c.end-c.start)):s.segments.reduce((n,c)=>n+c.end-c.start,0)):s.end-s.start;if(t==='speed')seconds/=s.speed;if(t==='motion')seconds=(seconds+s.freeze_end)*s.repeat;if(t==='transition')seconds-=Math.max(0,s.segments.length-1)*s.transition_duration;text=`预计 ${Math.max(0,seconds||0).toFixed(2)} 秒 · ${this.info.audioOnly?s.format.toUpperCase():'先预览，再下载'}`;}
  $('tool-input-hint').textContent=text;
 }
 payload(strict=false){
  const s=this.state,t=this.active;
  if(strict&&!this.form.reportValidity())return null;
  const base={kind:legacy.has(t)?t==='grade'?'render':t:'edit',tool:t};
  const clip={asset:s.asset,start:Number(s.start),end:Number(s.end),audio_track:Number(s.audio_track)};
  if(t==='transcribe'){if(strict&&!s.assets.length)throw Error('请选择至少一个要转写的素材。');return {...base,assets:s.assets,language:s.language,speakers:s.speakers===''?null:Number(s.speakers),audio_track:Number(s.audio_track),workers:Number(s.workers)};}
  if(t==='pack')return {...base,silence:Number(s.silence)};
  if(['timeline','analyze','review'].includes(t))return {...base,assets:s.asset?[s.asset]:[],start:Number(s.start),end:Number(s.end),...(t==='timeline'?{audio_track:Number(s.audio_track),frames:Number(s.frames)}:{})};
  const clips=this.info.multi?s.segments.map(c=>({...c})):s.asset?[clip]:[];
  if(strict){
   if(!clips.length)throw Error('请先选择素材。');
   for(const [i,c] of clips.entries()){const a=this.media.find(a=>a.id===c.asset);if(!a)throw Error(`请为片段 ${i+1} 选择素材。`);if(!Number.isFinite(c.start)||!Number.isFinite(c.end)||c.start<0||c.end<=c.start||c.end>a.duration+.02)throw Error(`片段 ${i+1} 的时间需位于 0–${a.duration.toFixed(2)} 秒之间，且结束晚于开始。`);}
   if(t==='subtitles'&&s.sub_source==='transcript'&&!this.media.find(a=>a.id===s.asset)?.transcribed_tracks?.includes(Number(s.audio_track)))throw Error('此音轨还没有转写。请先使用“语音转写”，或选择上传的 SRT。');
  }
  if(t==='render')return {...base,segments:clips,overlays:s.overlays,grade:s.grade,quality:s.quality,fps:s.legacy_fps,normalize_audio:s.normalize_audio,subtitles:s.legacy_subtitles,subtitle_asset:s.legacy_subtitles==='upload'?s.subtitle_asset||null:null};
  if(t==='grade')return {...base,segments:clips,grade:s.grade,quality:s.quality,fps:s.legacy_fps,normalize_audio:false};
  const [width,height]=this.dimensions();const plan={version:1,clips:clips.map(c=>({...c,fit:s.fit})),output:{width,height,fps:Number(s.fps),crf:Number(s.crf)}};
  const c=plan.clips[0];
  if(c&&t==='speed')c.speed=Number(s.speed);
  if(c&&t==='motion')Object.assign(c,{reverse:s.reverse,reverse_audio:s.reverse&&s.reverse_audio,freeze_end:Number(s.freeze_end),repeat:Number(s.repeat)});
  if(c&&t==='geometry'){Object.assign(c,{rotate:Number(s.rotate),flip:s.flip});if(s.crop)c.crop=[s.crop_x,s.crop_y,s.crop_w,s.crop_h].map(Number);}
  if(t==='transition'){if(strict&&clips.length<2)throw Error('转场至少需要两个片段。');plan.transition={type:s.transition,duration:Number(s.transition_duration)};}
  if(t==='split')Object.assign(plan,{layout:s.layout,split_audio:Number(s.split_audio)});
  const time=s.full?{}:{start:Number(s.effect_start),end:Number(s.effect_end)};
  if(t==='mask')plan.masks=[{x:Number(s.mask_x),y:Number(s.mask_y),width:Number(s.mask_w),height:Number(s.mask_h),mode:s.mask_mode,...time}];
  if(t==='titles')plan.titles=[{text:s.text,size:Number(s.size),position:s.position,color:s.color,background:s.background,...time}];
  if(t==='subtitles')plan.subtitles={...(s.sub_source==='upload'?{asset:s.subtitle_asset}:{from_transcripts:true,words_per_line:Number(s.words_per_line),...(s.highlight?{highlight:s.highlight_color}:{})}),size:Number(s.size),position:s.position,color:s.color,outline:Number(s.outline)};
  if(t==='mix')plan.audio={asset:s.music_asset,mode:s.mode,start:Number(s.music_start),end:Number(s.music_end),offset:Number(s.offset),volume:Number(s.volume),loop:s.loop,duck:s.mode==='mix'&&s.duck,fade_in:Number(s.fade_in),fade_out:Number(s.fade_out)};
  if(t==='audio'){plan.output={audio_only:true,format:s.format};plan.audio={volume:Number(s.clean_volume),denoise:s.denoise,normalize:s.normalize,highpass:Number(s.highpass),fade_in:Number(s.fade_in),fade_out:Number(s.fade_out)};}
  return {...base,edit_plan:plan};
 }
}
