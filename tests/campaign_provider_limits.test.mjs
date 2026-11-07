import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {setProviderLimits}=createRequire(import.meta.url)('./campaign_browser_inputs.cjs');
function fixture(){
 const inputs=[['generate','images',false],['edit','images',false],['video','video',false],['missing','other',true]].map(([operation,group,disabled])=>({
  operation,group,disabled,value:disabled?'0':'10',
  async getAttribute(name){return name==='data-operation'?this.operation:this.group;},
  async isDisabled(){return this.disabled;},async inputValue(){return this.value;},
  async fill(value){assert.equal(this.disabled,false);this.value=value;}
 }));
 return {inputs,page:{locator(selector){assert.equal(selector,'[data-operation]');return {all:async()=>inputs};}}};
}
test('approval cases clear all available defaults before freezing zero external scope',async()=>{
 const {page,inputs}=fixture();assert.deepEqual(await setProviderLimits(page,{}),{});
 assert.deepEqual(inputs.map(x=>x.value),['0','0','0','0']);
});
test('only declared limits survive, grouped by actual provider budget identity',async()=>{
 const {page,inputs}=fixture();assert.deepEqual(await setProviderLimits(page,{generate:1,edit:1}),{images:2});
 assert.deepEqual(inputs.map(x=>x.value),['1','1','0','0']);
});
test('unavailable and unknown providers stop the fixture before Run creation',async()=>{
 await assert.rejects(setProviderLimits(fixture().page,{missing:1}),e=>e.dependency===true);
 await assert.rejects(setProviderLimits(fixture().page,{unknown:1}),/Unknown fixture/);
});
