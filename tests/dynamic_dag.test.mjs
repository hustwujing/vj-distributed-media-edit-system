import test from 'node:test';
import assert from 'node:assert/strict';
import {layoutDag} from '../webapp/static/dynamic_dag.js';

const task=id=>({id,selected:true,spec:{name:id}});

test('tasks are layered after every upstream dependency',()=>{
 const graph=layoutDag([task('source'),task('audio'),task('mix')],[
  {upstream_task_id:'source',downstream_task_id:'mix'},
  {upstream_task_id:'audio',downstream_task_id:'mix'},
 ]);
 const nodes=Object.fromEntries(graph.nodes.map(node=>[node.id,node]));
 assert.ok(nodes.mix.x>nodes.source.x);assert.ok(nodes.mix.x>nodes.audio.x);
 assert.equal(graph.edges.length,2);
});

test('retired tasks and dangling dependencies are excluded',()=>{
 const retired={...task('old'),selected:false};
 const graph=layoutDag([task('kept'),retired],[{upstream_task_id:'old',downstream_task_id:'kept'}]);
 assert.deepEqual(graph.nodes.map(node=>node.id),['kept']);assert.deepEqual(graph.edges,[]);
});

test('control jobs use a separate lane and historic cycles remain visible',()=>{
 const graph=layoutDag([task('a'),task('b')],[
  {upstream_task_id:'a',downstream_task_id:'b'},
  {upstream_task_id:'b',downstream_task_id:'a'},
 ],[{id:'review',control_kind:'REVIEW'}]);
 assert.equal(graph.nodes.filter(node=>node.kind==='task').length,2);
 assert.equal(graph.nodes.find(node=>node.kind==='control').y,30);
 assert.ok(graph.nodes.filter(node=>node.kind==='task').every(node=>node.y>=112));
});
