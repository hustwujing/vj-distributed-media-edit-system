import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
const source=await readFile(new URL('../webapp/static/task_stop.js',import.meta.url),'utf8');
const {stopState}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const cancelled=(cleanup='complete',external=[])=>({status:'cancelled',execution:{cleanup:{state:cleanup},external_cleanup:external.map(state=>({state}))}});
test('active tasks can be stopped while confirmation and submission block duplicates',()=>{
 for(const status of ['queued','running','waiting','paused']){
  assert.equal(stopState({status}).disabled,false);
  for(const phase of ['confirming','submitting','awaiting'])assert.equal(stopState({status},phase).disabled,true);
 }
 assert.equal(stopState({status:'cancelling'}).disabled,true);
});
test('cancel acknowledgement does not mean local cleanup is complete',()=>{
 for(const phase of ['allocated','pending','cleaning']){const s=stopState(cancelled(phase));assert.ok(s.visible&&s.disabled&&s.busy);assert.match(s.label,/收尾/);}
 assert.equal(stopState(cancelled('quarantined')).label,'回收待处理');
 assert.equal(stopState({status:'cancelled'}).label,'停止待确认');
});
test('external cancellation must stay distinguishable from local completion',()=>{
 for(const phase of ['pending','running'])assert.equal(stopState(cancelled('complete',[phase])).label,'确认停止中…');
 for(const phase of ['unknown','unconfirmed','unsupported']){const s=stopState(cancelled('complete',[phase]));assert.ok(s.visible&&s.disabled);assert.equal(s.busy,false);assert.match(s.description,/尚未确认/);}
});
test('completed stops and naturally finished tasks do not offer repeated cancellation',()=>{
 assert.equal(stopState(cancelled('complete',['complete']),'awaiting').visible,false);
 assert.equal(stopState(cancelled('not_allocated')).visible,false);
 for(const status of ['succeeded','failed'])assert.equal(stopState({status},'awaiting').visible,false);
 assert.equal(stopState(null).visible,false);
});
