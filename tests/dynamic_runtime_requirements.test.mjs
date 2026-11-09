import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.document={querySelector:()=>({href:'http://localhost/'})};
const {confirmationDetails}=await import('../webapp/static/dynamic_results.js');

const decision=requirements=>({kind:'plan_confirmation',request:{delivery_targets:['result'],steps:[{
 type:'agent',name:'Code editor',objective:'Generate result',inputs:{},runtime_requirements:requirements
}]}});

test('plan confirmation shows real sandbox route dependencies and exact network scope',()=>{
 const cpu=confirmationDetails(decision({requires_gpu:false,python_modules:['numpy'],system_tools:['ffmpeg'],network_urls:['https://example.com/data']}));
 assert.match(cpu,/OpenSandbox Harness（CPU）/);assert.match(cpu,/numpy、ffmpeg/);assert.match(cpu,/https:\/\/example.com\/data/);
 const gpu=confirmationDetails(decision({requires_gpu:true,python_modules:[],system_tools:[],network_urls:[]}));
 assert.match(gpu,/bwrap（GPU \/ 宿主设备）/);assert.match(gpu,/不联网/);
});
