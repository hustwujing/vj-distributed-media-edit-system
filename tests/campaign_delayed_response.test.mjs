import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {holdNextResponse}=createRequire(import.meta.url)('./campaign_browser_inputs.cjs');
function fixture(){
 const value={handler:null,removed:false,fulfilled:false};
 value.page={async route(pattern,handler,options){assert.equal(options.times,1);value.handler=handler;},
  async unroute(pattern,handler){assert.equal(handler,value.handler);value.removed=true;}};
 value.route={async fetch(){return {status:200};},async fulfill(){assert.equal(value.removed,false);value.fulfilled=true;}};
 return value;
}
test('teardown drains a held response before removing its handler',async()=>{
 const f=fixture(),delayed=await holdNextResponse(f.page,'route');
 const processing=f.handler(f.route);await delayed.wait();assert.equal(f.fulfilled,false);
 await delayed.finish();await processing;assert.equal(f.fulfilled,true);assert.equal(f.removed,true);
});
test('route errors are awaited by the owning case rather than rejected callbacks',async()=>{
 const f=fixture();f.route.fulfill=async()=>{throw Error('fulfill failed');};
 const delayed=await holdNextResponse(f.page,'route'),processing=f.handler(f.route);
 await delayed.wait();await assert.rejects(delayed.finish(),/fulfill failed/);
 await processing;assert.equal(f.removed,true);
});
test('fetch failure wakes the case and remains available during teardown',async()=>{
 const f=fixture();f.route.fetch=async()=>{throw Error('fetch failed');};
 const delayed=await holdNextResponse(f.page,'route'),processing=f.handler(f.route);
 await assert.rejects(delayed.wait(),/fetch failed/);await assert.rejects(delayed.finish(),/fetch failed/);await processing;
});
test('cleanup is safe when upload failed before the route was requested',async()=>{
 const f=fixture(),delayed=await holdNextResponse(f.page,'route');await delayed.finish();assert.equal(f.removed,true);
});
