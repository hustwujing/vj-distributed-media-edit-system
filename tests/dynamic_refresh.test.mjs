import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the actual page refresh function with out-of-order HTTP completion.
const source=fs.readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
const body=source.slice(source.indexOf('async function refresh(){'),source.indexOf("window.addEventListener('pagehide'"));
const restore=source.slice(source.indexOf('function restoreInputDraft('),source.indexOf('function error('));
function fixture(){
 const requests=[],timers=new Map(),renders=[];let nextTimer=0;
 const nodes=new Map();
 const context=vm.createContext({stopped:false,refreshEpoch:0,currentId:'run',current:{state:'ACTIVE'},newRunMode:false,
  timer:null,cursorTimer:null,eventStream:{},names:{},labels:{},graphEvents:[],path:value=>value,esc:value=>value,error:()=>{},
  readDraft:()=>null,renderAttachmentChips:()=>{},
  terminalRun:state=>['SUCCEEDED','FAILED','CANCELLED'].includes(state),
  $:id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},options:[],value:'',setAttribute:()=>{},querySelectorAll:()=>[]});return nodes.get(id);},
  api:url=>url==='/runs'?new Promise((resolve,reject)=>requests.push({resolve,reject})):
   Promise.resolve(url.includes('/events')?{events:[]}:{id:'run',state:'ACTIVE'}),
  render:run=>{context.current=run;renders.push(run);},
  setTimeout:fn=>{const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)
 });
 vm.runInContext('let inputDraftRun=null;\n'+restore+'\n'+body,context);
 const listing={runs:[{id:'run',state:'ACTIVE',goal:'Current task',graph_version:0}]};
 return {context,requests,timers,renders,listing};
}
for(const failure of [false,true])test(`stale ${failure?'failed':'successful'} refresh cannot cancel current polling`,async()=>{
 const f=fixture(),older=f.context.refresh(),newer=f.context.refresh();
 f.requests[1].resolve(f.listing);await newer;assert.equal(f.timers.size,1);
 if(failure)f.requests[0].reject(Error('old HTTP error'));else f.requests[0].resolve(f.listing);
 await older;assert.equal(f.timers.size,1);assert.equal(f.renders.length,1);
});
test('page shutdown does not schedule a replacement timer',async()=>{
 const f=fixture(),pending=f.context.refresh();f.context.stopped=true;
 f.requests[0].resolve(f.listing);await pending;assert.equal(f.timers.size,0);
});
test('initial new form restores saved input once and polling preserves ongoing typing',async()=>{
 const f=fixture();f.context.currentId=null;f.context.current=null;f.context.eventStream=null;
 f.context.$('dynamic-input-form').dataset.run='new';
 f.context.readDraft=()=>({text:'saved draft',requestId:'saved-request',assets:['asset']});
 f.context.$('dynamic-input-assets').options=[{value:'asset',selected:false}];
 const initial=f.context.refresh();f.requests[0].resolve({runs:[]});await initial;
 assert.equal(f.context.$('dynamic-input-content').value,'saved draft');
 assert.equal(f.context.$('dynamic-input-form').dataset.requestId,'saved-request');
 assert.equal(f.context.$('dynamic-input-assets').options[0].selected,true);
 f.context.$('dynamic-input-content').value='still typing';
 const polling=f.context.refresh();f.requests[1].resolve({runs:[]});await polling;
 assert.equal(f.context.$('dynamic-input-content').value,'still typing');
});
test('initial server prompt survives when no browser draft exists',()=>{
 const f=fixture();f.context.$('dynamic-input-form').dataset.run='new';
 f.context.$('dynamic-input-content').value='server example prompt';
 f.context.restoreInputDraft('new',false);
 assert.equal(f.context.$('dynamic-input-content').value,'server example prompt');
 f.context.restoreInputDraft('another-run',false);
 assert.equal(f.context.$('dynamic-input-content').value,'');
});
test('restored terminal-run draft cannot reactivate task targets',()=>{
 const f=fixture();f.context.readDraft=()=>({text:'next input',targets:['old-task']});
 f.context.$('dynamic-input-targets').options=[{value:'old-task',selected:true}];
 f.context.restoreInputDraft('finished-run',false);
 assert.equal(f.context.$('dynamic-input-content').value,'next input');
 assert.equal(f.context.$('dynamic-input-targets').options[0].selected,false);
});
