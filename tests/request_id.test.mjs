import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {randomRequestId} from '../webapp/static/request_id.js';
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
test('uses native UUID when supported',()=>{assert.equal(randomRequestId({randomUUID(){return 'native-id';}}),'native-id');});
test('HTTP fallback uses secure randomness and generates distinct UUID v4 keys',()=>{
 const source={getRandomValues:values=>webcrypto.getRandomValues(values)};
 const ids=Array.from({length:100},()=>randomRequestId(source));
 for(const id of ids)assert.match(id,uuid);
 assert.equal(new Set(ids).size,100);
});
test('fallback preserves entropy while setting version and variant',()=>{
 assert.equal(randomRequestId({randomUUID:undefined,getRandomValues:values=>values.fill(255)}),'ffffffff-ffff-4fff-bfff-ffffffffffff');
 assert.throws(()=>randomRequestId({}),/安全随机数/);
});
