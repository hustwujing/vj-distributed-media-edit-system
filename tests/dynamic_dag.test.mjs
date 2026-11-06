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
 ],[{id:'review',control_kind:'REVIEW'}],{run:{state:'ACTIVE',graph_version:1},reviews:[{task_id:'review',subject_task:'b'}]});
 assert.equal(graph.nodes.filter(node=>node.kind==='task').length,2);
 assert.equal(graph.nodes.find(node=>node.kind==='control').y,30);
 assert.equal(graph.nodes.find(node=>node.kind==='run').y,30);
 assert.ok(graph.edges.some(edge=>edge.kind==='control'&&edge.upstream==='b'&&edge.downstream==='review'));
 assert.ok(graph.nodes.filter(node=>node.kind==='task').every(node=>node.y>=112));
});

test('replanning control edges return to manager and connect applied revisions',()=>{
 const tasks=[
  {...task('source'),revision_graph_version:1},
  {...task('result'),revision_graph_version:2},
 ];
 const controls=[
  {id:'plan-1',control_kind:'PLAN'},
  {id:'review',control_kind:'REVIEW'},
  {id:'plan-2',control_kind:'PLAN'},
 ];
 const graph=layoutDag(tasks,[{upstream_task_id:'source',downstream_task_id:'result'}],controls,{
  run:{state:'ACTIVE',graph_version:2},
  reviews:[{task_id:'review',subject_task:'source'}],
  planners:[
   {task_id:'plan-1',applied_graph_version:1,trigger_task_ids:[],trigger_control_job_ids:[]},
   {task_id:'plan-2',applied_graph_version:2,trigger_task_ids:[],trigger_control_job_ids:['review']},
  ],
 });
 const controlEdges=graph.edges.filter(edge=>edge.kind==='control').map(edge=>`${edge.upstream}->${edge.downstream}`);
 assert.ok(controlEdges.includes('__run__->plan-1'));
 assert.ok(controlEdges.includes('plan-1->source'));
 assert.ok(controlEdges.includes('source->review'));
 assert.ok(controlEdges.includes('review->plan-2'));
 assert.ok(controlEdges.includes('plan-2->result'));
});
