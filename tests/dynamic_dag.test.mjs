import test from 'node:test';
import assert from 'node:assert/strict';
import {layoutDag,dagEdgePath} from '../webapp/static/dynamic_dag.js';

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
 assert.ok(graph.nodes.find(node=>node.kind==='control'));
 assert.ok(graph.nodes.find(node=>node.kind==='run'));
 assert.ok(graph.edges.some(edge=>edge.kind==='control'&&edge.upstream==='b'&&edge.downstream==='review'));
 assert.equal(new Set(graph.nodes.map(node=>`${node.x}:${node.y}`)).size,graph.nodes.length);
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
 const nodes=Object.fromEntries(graph.nodes.map(node=>[node.id,node]));
 for(const edge of graph.edges)assert.ok(nodes[edge.downstream].level>nodes[edge.upstream].level,`${edge.upstream} must precede ${edge.downstream}`);
});

test('long dependency chains turn right to left, then left to right at viewport boundaries',()=>{
 const tasks=Array.from({length:11},(_,i)=>task(`t${i}`)),edges=tasks.slice(1).map((t,i)=>({upstream_task_id:tasks[i].id,downstream_task_id:t.id}));
 const graph=layoutDag(tasks,edges,[],{viewportWidth:900}),n=graph.nodes;
 assert.equal(graph.columns,4);
 assert.ok(n[0].x<n[1].x&&n[1].x<n[2].x&&n[2].x<n[3].x);
 assert.equal(n[3].x,n[4].x);assert.ok(n[4].y>n[3].y);
 assert.ok(n[4].x>n[5].x&&n[5].x>n[6].x&&n[6].x>n[7].x);
 assert.equal(n[7].x,n[8].x);assert.ok(n[8].y>n[7].y);assert.ok(n[8].x<n[9].x);
 for(const node of n)assert.ok(node.x>=0&&node.x+node.width<=900);
 assert.match(dagEdgePath(n[4],n[5]),new RegExp(`^M ${n[4].x} `),'RTL edge exits the left port');
 assert.match(dagEdgePath(n[3],n[4]),new RegExp(`^M ${n[3].x+n[3].width/2} ${n[3].y+n[3].height} `),'row turn exits the bottom port');
});

test('parallel branches do not overlap and narrow screens fit a single column',()=>{
 const tasks=Array.from({length:16},(_,i)=>task(`t${i}`)),edges=tasks.slice(4).map((t,i)=>({upstream_task_id:tasks[i].id,downstream_task_id:t.id}));
 for(const viewportWidth of [375,760,1180]){
  const graph=layoutDag(tasks,edges,[],{viewportWidth});
  assert.equal(graph.width,viewportWidth);if(viewportWidth===375)assert.equal(graph.columns,1);
  for(const a of graph.nodes){
   assert.ok(a.x>=0&&a.x+a.width<=viewportWidth);
   for(const b of graph.nodes)if(a.id!==b.id)assert.ok(a.x+a.width<=b.x||b.x+b.width<=a.x||a.y+a.height<=b.y||b.y+b.height<=a.y,`${a.id} overlaps ${b.id}`);
  }
 }
});

test('historic control calls do not depend on replacement or newer task revisions',()=>{
 const tasks=[{...task('old'),selected:false,revision_graph_version:1},{...task('revised'),revision_graph_version:3}];
 const jobs=[{id:'old-review',control_kind:'REVIEW'},{id:'final-review',control_kind:'REVIEW'},{id:'repair-plan',control_kind:'PLAN'}];
 const graph=layoutDag(tasks,[],jobs,{reviews:[{task_id:'old-review',subject_task:'old',graph_version:1},
  {task_id:'final-review',subject_task:'',graph_version:2}],planners:[{task_id:'repair-plan',base_graph_version:2,applied_graph_version:3,trigger_task_ids:['revised']}]});
 assert.ok(graph.edges.some(edge=>edge.upstream==='repair-plan'&&edge.downstream==='revised'));
 assert.ok(!graph.edges.some(edge=>edge.upstream==='revised'),'A new revision cannot cause an older review or the plan that created itself');
});
