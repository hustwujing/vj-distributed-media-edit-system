/* Optional browser fields can be hidden when no assets/tasks exist yet. */
const assert=require('node:assert/strict');
async function selectOptional(locator,values){
 if(values.length||await locator.isVisible())return locator.selectOption(values);
 if(await locator.count())assert.equal(await locator.evaluate(node=>node.selectedOptions.length),0,
  'A hidden optional selector retained an unexpected selection');
}
module.exports={selectOptional};
