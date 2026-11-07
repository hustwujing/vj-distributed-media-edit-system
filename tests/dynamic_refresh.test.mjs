import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the actual page refresh function with out-of-order HTTP completion.
const source=fs.readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
const body=source.slice(source.indexOf('async function refresh(){'),source.indexOf("window.addEventListener('pagehide'"));
function fixture(){
 const requests=[],timers=new Map(),renders=[];let nextTimer=0;
 const nodes=new Map();
 const context=vm.createContext({stopped:false,refreshEpoch:0,currentId:'run',current:{state:'ACTIVE'},newRunMode:false,
  timer:null,eventStream:{},names:{},labels:{},graphEvents:[],path:value=>value,esc:value=>value,error:()=>{},
  terminalRun:state=>['SUCCEEDED','FAILED','CANCELLED'].includes(state),
  $:id=>{if(!nodes.has(id))nodes.set(id,{querySelectorAll:()=>[]});return nodes.get(id);},
  api:url=>url==='/runs'?new Promise((resolve,reject)=>requests.push({resolve,reject})):
   Promise.resolve(url.includes('/events')?{events:[]}:{id:'run',state:'ACTIVE'}),
  render:run=>{context.current=run;renders.push(run);},
  setTimeout:fn=>{const id=++nextTimer;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)
 });
 vm.runInContext(body,context);
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
