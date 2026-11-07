/* Optional browser fields can be hidden when no assets/tasks exist yet. */
const assert=require('node:assert/strict');
async function selectOptional(locator,values){
 if(values.length||await locator.isVisible())return locator.selectOption(values);
 if(await locator.count())assert.equal(await locator.evaluate(node=>node.selectedOptions.length),0,
  'A hidden optional selector retained an unexpected selection');
}
async function setProviderLimits(page,requested){
 const budgets={},seen=new Set();
 for(const input of await page.locator('[data-operation]').all()){
  const operation=await input.getAttribute('data-operation'),limit=requested[operation]??0;
  seen.add(operation);assert.ok(Number.isInteger(limit)&&limit>=0&&limit<=10,'Invalid fixture provider limit');
  if(await input.isDisabled()){
   if(limit)throw Object.assign(Error('Required provider is not configured: '+operation),{dependency:true});
   assert.equal(Number(await input.inputValue()),0,'Disabled provider retained a nonzero limit');
  }else await input.fill(String(limit));
  if(limit){const group=await input.getAttribute('data-budget-group');assert.ok(group);budgets[group]=(budgets[group]||0)+limit;}
 }
 for(const operation of Object.keys(requested))assert.ok(seen.has(operation),'Unknown fixture provider operation: '+operation);
 return budgets;
}
async function holdNextResponse(page,pattern){
 let release,markHeld,markFinished,started=false,failure;
 const held=new Promise(resolve=>{markHeld=resolve;}),released=new Promise(resolve=>{release=resolve;}),finished=new Promise(resolve=>{markFinished=resolve;});
 const handler=async route=>{
  started=true;
  try{const response=await route.fetch();markHeld();await released;await route.fulfill({response});}
  catch(error){failure=error;markHeld();}
  finally{markFinished();}
 };
 await page.route(pattern,handler,{times:1});
 return {release,async wait(){await held;if(failure)throw failure;},async finish(){
  release();if(started)await finished;
  await page.unroute(pattern,handler);
  if(failure)throw failure;
 }};
}
module.exports={selectOptional,setProviderLimits,holdNextResponse};
