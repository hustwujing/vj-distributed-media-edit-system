import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../webapp/static/dynamic.js',import.meta.url),'utf8');
const streamCode=source.slice(source.indexOf('function startRunStream('),source.indexOf('\nfunction renderReusable('));
function fixture(){
 const streams=[],state={current:null,streamRun:null,eventStream:null,completedStreams:new Set(),liveEvents:[],agentTextEvents:[],agentStreamTurn:{},lastAgentOutputAt:0,
  $:()=>({textContent:''}),href:x=>x,path:x=>x,renderAgentTranscript:()=>{},queueMicrotask,
  EventSource:class{constructor(url){this.url=url;this.listeners={};streams.push(this);}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}},
 };
 state.refresh=async()=>state.startRunStream(state.current.id);
 vm.createContext(state);vm.runInContext(streamCode,state);
 return {streams,select(id){state.current={id,state:'SUCCEEDED'};state.startRunStream(id);},done(){streams.at(-1).listeners.done();},state};
}

test('completed Run stays closed after done refresh instead of replaying forever',async()=>{
 const f=fixture();f.select('a');f.done();await Promise.resolve();
 assert.equal(f.streams.length,1);
 assert.equal(f.streams[0].closed,true);
 f.select('a');assert.equal(f.streams.length,1);
});

test('switching away and back replays history once; an explicit resume can reconnect',async()=>{
 const f=fixture();f.select('a');f.done();await Promise.resolve();
 f.select('b');f.done();await Promise.resolve();
 f.select('a');assert.equal(f.streams.length,3);
 f.done();await Promise.resolve();assert.equal(f.streams.length,3);
 f.state.completedStreams.delete('a');f.select('a');assert.equal(f.streams.length,4);
});
