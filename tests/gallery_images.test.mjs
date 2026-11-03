import test from 'node:test';import assert from 'node:assert/strict';
import {PosterLoader} from '../webapp/static/gallery_images.js';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const img=name=>({dataset:{poster:name},isConnected:true,alt:name});
const root=images=>({querySelectorAll:()=>images});
const response=()=>({status:200,ok:true,blob:async()=>new Blob(['image'])});
test('a burst of gallery thumbnails has at most two live requests',async()=>{
 let active=0,peak=0;const waiting=[];const images=Array.from({length:7},(_,i)=>img(String(i)));
 const loader=new PosterLoader({fetcher:()=>new Promise(resolve=>{active++;peak=Math.max(peak,active);waiting.push(()=>{active--;resolve(response());});}),createURL:()=>`blob:${Math.random()}`,revokeURL:()=>{}});
 loader.show(root(images));assert.equal(active,2);
 for(let i=0;i<7;i++){assert.ok(waiting.length);waiting.shift()();await tick();}
 assert.equal(peak,2);assert.equal(loader.active,0);assert.ok(images.every(image=>image.dataset.posterState==='loaded'));loader.clear();
});
test('a capacity rejection is retried within the same bounded slot',async()=>{
 let calls=0;const waits=[];const image=img('poster');
 const loader=new PosterLoader({fetcher:async()=>++calls===1?{status:503,ok:false,headers:{get:()=> '1'},body:{cancel:async()=>{}}}:response(),wait:async ms=>waits.push(ms),createURL:()=> 'blob:accepted',revokeURL:()=>{}});
 loader.show(root([image]));await tick();assert.equal(calls,2);assert.deepEqual(waits,[1000]);assert.equal(image.src,'blob:accepted');loader.clear();
});
test('late responses from an old filter cannot attach or exceed the active limit',async()=>{
 const waiting=[];const old=img('old'),next=img('new');
 const loader=new PosterLoader({limit:1,fetcher:()=>new Promise(resolve=>waiting.push(resolve)),createURL:()=> 'blob:new',revokeURL:()=>{}});
 loader.show(root([old]));loader.show(root([next]));assert.equal(waiting.length,1);
 waiting.shift()(response());await tick();assert.equal(old.src,undefined);assert.equal(waiting.length,1);
 waiting.shift()(response());await tick();assert.equal(next.src,'blob:new');loader.clear();
});
