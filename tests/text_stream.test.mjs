import test from 'node:test';import assert from 'node:assert/strict';
import {mergeTurn,applyText,isTyping} from '../webapp/static/text_stream.js';
import {runningWork,workLabel,mergeWork,settleTool} from '../webapp/static/work_status.js';
test('a delayed snapshot cannot roll live text or terminal status backwards',()=>{
 const live={status:'running',text:'完整说明',text_version:[2,9],text_phase:'idle',updated:100};
 assert.equal(mergeTurn(live,{status:'running',text:'完',text_version:[2,1],updated:101}).text,'完整说明');
 assert.equal(mergeTurn({...live,status:'succeeded',updated:102},{...live,status:'running',updated:101}).status,'succeeded');
});
test('completed message replaces a partial tail immediately and stops typing',()=>{
 const partial={status:'running',text:'我',text_version:[1,1]};
 const full=applyText(partial,{text:'我现在检查蒙版及透明边缘。',text_version:[1,2],text_phase:'idle'},true);
 assert.equal(full.text,'我现在检查蒙版及透明边缘。');assert.equal(isTyping(full),false);
 assert.equal(applyText(full,{delta:'重复',text_version:[1,1],base_version:[1,0]}).text,full.text);
});
test('a new attempt replaces previous output, and gaps request reconciliation',()=>{
 let current={status:'running',text:'旧内容',text_version:[1,20]};
 current=applyText(current,{text:'新',text_version:[2,1],text_phase:'streaming'},true);
 assert.equal(current.text,'新');assert.equal(isTyping(current),true);
 assert.equal(applyText(current,{delta:'缺前缀',text_version:[2,5],base_version:[2,4]})._streamGap,true);
 const next=applyText(current,{delta:'结果',text_version:[2,2],base_version:[2,1],text_phase:'streaming'});
 assert.equal(next.text,'新结果');assert.equal(isTyping(next,Date.now()+2000),false);
});
test('terminal text is authoritative even when earlier live deltas arrive later',()=>{
 const ended={status:'succeeded',text:'最终回答',text_version:[1,3],updated:5};
 assert.deepEqual(applyText(ended,{delta:'迟到',text_version:[1,5],base_version:[1,4]}),ended);
 assert.equal(mergeTurn({status:'running',text:'中途说明',text_version:[1,5],updated:4},ended).text,'最终回答');
});
test('actors are distinct and parallel workflow steps stay visible',()=>{
 const items=[{id:'a',actor:'sandbox_agent',phase:'model_wait',label:'等待模型响应',status:'running',updated:1},
 {id:'b',actor:'sandbox_agent',phase:'writing',label:'编写剪辑脚本',status:'running',updated:2},
 {id:'c',actor:'host',phase:'submit',label:'提交操作',operation:'image.remove_bg',status:'running',step:'one',updated:3},
 {id:'d',actor:'workflow_sandbox',phase:'executing',label:'执行代码',status:'running',step:'two',updated:4}];
 const active=runningWork({status:'running'},items);assert.equal(active.length,4);assert.ok(active.some(x=>x.id==='a'));assert.match(workLabel(items[2]),/宿主程序.*image.remove_bg/);
 assert.match(workLabel({actor:'manager_agent',label:'模型请求已提交，等待首包',operation:'claude-sonnet-5-5'}),/任务规划师.*等待首包.*claude-sonnet-5-5/);
 assert.deepEqual(runningWork({status:'succeeded'},items),[]);
});

test('submitted external work remains visible between scheduler polls',()=>{
 const items=runningWork({status:'waiting',workflow:{steps:[{id:'video',status:'waiting',wait_reason:'provider',operation:'video.generate_from_image',updated:10}]}},[]);
 assert.equal(items.length,1);assert.equal(items[0].actor,'external_service');assert.equal(items[0].started,undefined);assert.match(workLabel(items[0]),/等待上游任务完成/);
});

test('replayed activity cannot resurrect a completed tool',()=>{
 const done={id:'tool:one',updated:20,status:'completed'};
 assert.equal(mergeWork(done,{id:'tool:one',updated:10,status:'running'}).status,'completed');
 assert.equal(mergeWork(done,{id:'tool:one',updated:20,status:'running'}).status,'completed');
});
test('a finished activity cannot be revived by a later timestamp or older sequence',()=>{
 const done={id:'a',status:'completed',sequence:4,updated:100};
 assert.equal(mergeWork(done,{...done,status:'running',sequence:3,updated:200}),done);
});
test('concurrent operations from the same actor remain visible',()=>{
 const work=runningWork({status:'running'},[{id:'a',actor:'host',status:'running',phase:'checkpoint',updated:1},{id:'b',actor:'host',status:'running',phase:'prepare_environment',updated:2}]);
 assert.equal(work.length,2);
});

test('late events from a finished attempt do not become current work',()=>{
 const item={id:'old',actor:'host',status:'running',execution:{attempt_id:'a'},updated:1000};
 assert.deepEqual(runningWork({status:'running',activity_attempts:[{id:'a',status:'finished'}]},[item]),[]);
});

test('status snapshots preserve the locally measured last event time',()=>{
 assert.equal(mergeTurn({updated:1,_lastEventAt:123},{updated:2,status:'running'})._lastEventAt,123);
});

test('completion and failure beat an earlier receipt clock when their sequence advances',()=>{
 for(const status of ['completed','failed']){
  const before={id:'fast',status:'running',sequence:1,updated:100.025};
  const after={id:'fast',status,sequence:2,updated:100};
  assert.equal(mergeWork(before,after),after);
  assert.equal(mergeWork(after,{...before,updated:200}),after);
 }
});
test('ordered phase updates survive clock rollback and stale high-clock events do not win',()=>{
 const old={id:'a',status:'running',sequence:2,updated:100};
 const next={...old,phase:'responding',sequence:3,updated:99};
 assert.equal(mergeWork(old,next),next);assert.equal(mergeWork(next,{...old,updated:200}),next);
});

test('a stopped attempt without an end receipt is interrupted, never successful',()=>{
 const tool={id:'read',status:'running',sequence:1,updated:100,execution:{attempt_id:'old'}};
 const ended=settleTool({status:'running',activity_attempts:[{id:'old',status:'expired'}]},tool);
 assert.equal(ended.status,'interrupted');assert.equal(ended.sequence,1);
 assert.equal(settleTool({status:'running',activity_attempts:[{id:'old',status:'running'}]},tool),tool);
 assert.equal(settleTool({status:'succeeded'},tool).status,'interrupted');
 assert.equal(mergeWork(ended,{...tool,status:'completed',sequence:2,updated:99}).status,'completed');
});
