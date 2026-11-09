import test from 'node:test';
import assert from 'node:assert/strict';
import {executionPlace} from '../webapp/static/dynamic_locations.js';

test('locations use recorded execution evidence, never task type or a guessed backend',()=>{
 assert.equal(executionPlace({type:'agent'}).label,'未记录');
 assert.equal(executionPlace({execution_location:'unallocated'}).label,'待分配');
 assert.equal(executionPlace({execution_location:'opensandbox_unconfirmed'}).label,'实例待确认');
 assert.equal(executionPlace({execution_location:'opensandbox'}).label,'OpenSandbox');
 assert.equal(executionPlace({execution_location:'bwrap_sandbox'}).label,'bwrap');
 const expert=executionPlace({execution_location:'agent_worker',tool_location:'bwrap_sandbox'});
 assert.equal(expert.label,'宿主 · bwrap');assert.match(expert.detail,/程序执行：Agent Worker 宿主机；工具执行：bwrap 沙箱/);
 const remoteTools=executionPlace({execution_location:'agent_worker',tool_location:'opensandbox'});
 assert.equal(remoteTools.label,'宿主 · OpenSandbox');
});

test('external processing, uncertain submission and completed local collection remain distinct',()=>{
 const item={execution_location:'executor_host',status:'waiting',provider_execution_state:'running'};
 assert.equal(executionPlace(item).label,'外部服务');assert.match(executionPlace(item).detail,/Executor 宿主机.*外部服务：处理中/);
 assert.equal(executionPlace({...item,provider_execution_state:'unknown'}).label,'宿主');
 assert.match(executionPlace({...item,provider_execution_state:'unknown'}).detail,/提交结果未确认/);
 const collected=executionPlace({...item,status:'succeeded',execution_location:'bwrap_sandbox',provider_execution_state:'succeeded'});
 assert.equal(collected.label,'bwrap');assert.match(collected.detail,/外部服务：已完成/);
 assert.equal(executionPlace({},true).label,'控制面');
});
