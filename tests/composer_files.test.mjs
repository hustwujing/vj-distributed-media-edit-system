import test from 'node:test';
import assert from 'node:assert/strict';
import {clipboardFiles} from '../webapp/static/composer_files.js';

test('clipboard files prefer the browser file list',()=>{
 const files=[{name:'screen.png'}];
 assert.deepEqual(clipboardFiles({files,items:[{kind:'file',getAsFile:()=>({name:'duplicate.png'})}]}),files);
});

test('clipboard item fallback keeps only real files',()=>{
 const image={name:'pasted-image.png'},document={name:'brief.pdf'};
 const result=clipboardFiles({files:[],items:[
  {kind:'string',getAsFile:()=>null},
  {kind:'file',getAsFile:()=>image},
  {kind:'file',getAsFile:()=>null},
  {kind:'file',getAsFile:()=>document},
 ]});
 assert.deepEqual(result,[image,document]);
});
