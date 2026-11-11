const assets={
 pdf:new URL('vendor/pdfjs/pdf.min.mjs',import.meta.url).href,
 pdfWorker:new URL('vendor/pdfjs/pdf.worker.min.mjs',import.meta.url).href,
 jszip:new URL('vendor/docx/jszip.min.js',import.meta.url).href,
 docx:new URL('vendor/docx/docx-preview.min.js',import.meta.url).href,
 xlsx:new URL('vendor/xlsx/xlsx.full.min.js',import.meta.url).href,
};
const scripts=new Map();

function loadScript(url,globalName){
 if(globalThis[globalName])return Promise.resolve(globalThis[globalName]);
 if(!scripts.has(url))scripts.set(url,new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=url;script.onload=()=>globalThis[globalName]?resolve(globalThis[globalName]):reject(new Error('预览组件未正确加载'));script.onerror=()=>reject(new Error('预览组件加载失败'));document.head.append(script);}));
 return scripts.get(url);
}

export function documentKind(item){
 const type=(item.type||'').toLowerCase(),ext=(item.name||'').split('.').at(-1).toLowerCase();
 if(type==='application/pdf'||ext==='pdf')return 'pdf';
 if(type==='application/vnd.openxmlformats-officedocument.wordprocessingml.document'||ext==='docx')return 'word';
 if(type==='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'||type==='application/vnd.ms-excel'||['xlsx','xls'].includes(ext))return 'excel';
 return '';
}

function loading(stage,text){stage.innerHTML='';const node=document.createElement('div');node.className='loading-state';node.textContent=text;stage.append(node);}

async function readDocument(url,maximum=64*1024*1024){
 const response=await fetch(url,{credentials:'same-origin'});if(!response.ok)throw new Error('无法读取文档文件');
 const declared=Number(response.headers.get('content-length')||0);if(declared>maximum)throw new Error('文件超过 64 MB 在线预览上限');
 if(!response.body){const value=await response.arrayBuffer();if(value.byteLength>maximum)throw new Error('文件超过 64 MB 在线预览上限');return value;}
 const reader=response.body.getReader(),chunks=[];let size=0;
 try{for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>maximum)throw new Error('文件超过 64 MB 在线预览上限');chunks.push(value);}}
 finally{reader.releaseLock();}
 const data=new Uint8Array(size);let offset=0;for(const chunk of chunks){data.set(chunk,offset);offset+=chunk.byteLength;}return data.buffer;
}

async function renderPdf(stage,url,current){
 const pdfjs=await import(assets.pdf);pdfjs.GlobalWorkerOptions.workerSrc=assets.pdfWorker;
 const pdf=await pdfjs.getDocument({data:new Uint8Array(await readDocument(url))}).promise;if(!current())return;
 const scroll=document.createElement('div');scroll.className='document-preview-scroll pdf-preview';stage.replaceChildren(scroll);
 const count=Math.min(pdf.numPages,100);
 for(let number=1;number<=count;number++){
  const page=await pdf.getPage(number);if(!current())return;
  const initial=page.getViewport({scale:1}),scale=Math.max(.25,Math.min(2,(Math.max(320,stage.clientWidth)-32)/initial.width));
  const viewport=page.getViewport({scale}),canvas=document.createElement('canvas'),context=canvas.getContext('2d',{alpha:false});
  canvas.width=Math.ceil(viewport.width);canvas.height=Math.ceil(viewport.height);canvas.setAttribute('aria-label',`PDF 第 ${number} 页`);scroll.append(canvas);
  await page.render({canvasContext:context,viewport}).promise;
 }
 if(pdf.numPages>count){const note=document.createElement('p');note.className='document-preview-limit';note.textContent=`在线预览前 ${count} 页，完整内容请下载查看。`;scroll.append(note);}
 return `${pdf.numPages} 页 PDF`;
}

async function renderWord(stage,url,current){
 await loadScript(assets.jszip,'JSZip');const docx=await loadScript(assets.docx,'docx');
 const data=await readDocument(url);if(!current())return;
 const scroll=document.createElement('div');scroll.className='document-preview-scroll word-preview';stage.replaceChildren(scroll);
 await docx.renderAsync(data,scroll,undefined,{className:'docx-page',inWrapper:true,breakPages:true,renderHeaders:true,renderFooters:true});
 return 'Word 文档';
}

function renderSheet(root,XLSX,book,name){
 const sheet=book.Sheets[name],rows=XLSX.utils.sheet_to_json(sheet,{header:1,raw:false,defval:''}).slice(0,1000),columns=Math.min(100,Math.max(0,...rows.map(row=>row.length)));
 const viewport=document.createElement('div');viewport.className='spreadsheet-scroll';const table=document.createElement('table'),body=document.createElement('tbody');
 rows.forEach(row=>{const tr=document.createElement('tr');for(let column=0;column<columns;column++){const cell=document.createElement('td');cell.textContent=String(row[column]??'');tr.append(cell);}body.append(tr);});table.append(body);viewport.append(table);
 if((sheet['!ref']&&XLSX.utils.decode_range(sheet['!ref']).e.r+1>1000)||columns===100){const note=document.createElement('p');note.className='document-preview-limit';note.textContent='在线预览最多 1000 行、100 列，完整内容请下载查看。';viewport.append(note);}
 root.querySelector('.spreadsheet-scroll')?.remove();root.append(viewport);
}

async function renderExcel(stage,url,current){
 const XLSX=await loadScript(assets.xlsx,'XLSX'),book=XLSX.read(await readDocument(url),{type:'array',cellDates:true});if(!current())return;
 const root=document.createElement('div');root.className='document-preview-scroll spreadsheet-preview';const tabs=document.createElement('nav');root.append(tabs);stage.replaceChildren(root);
 book.SheetNames.forEach((name,index)=>{const button=document.createElement('button');button.type='button';button.textContent=name;button.className=index===0?'selected':'';button.onclick=()=>{tabs.querySelectorAll('button').forEach(item=>item.classList.toggle('selected',item===button));renderSheet(root,XLSX,book,name);};tabs.append(button);});
 if(book.SheetNames.length)renderSheet(root,XLSX,book,book.SheetNames[0]);else throw new Error('Excel 文件中没有工作表');
 return `${book.SheetNames.length} 个工作表`;
}

export async function renderDocumentPreview(stage,item,current){
 const kind=documentKind(item);if(!kind)return false;
 loading(stage,kind==='pdf'?'正在加载 PDF…':kind==='word'?'正在加载 Word 文档…':'正在加载 Excel 工作簿…');
 try{
  const detail=await (kind==='pdf'?renderPdf(stage,item.url,current):kind==='word'?renderWord(stage,item.url,current):renderExcel(stage,item.url,current));
  return current()?{kind,detail}:false;
 }catch(error){
  if(current()){stage.innerHTML='';const message=document.createElement('div');message.className='preview-empty document-preview-error';const title=document.createElement('h3');title.textContent='文档在线预览失败';const body=document.createElement('p');body.textContent=(error?.message||'文件无法解析')+'，可以下载原文件查看。';message.append(title,body);stage.append(message);}
  return current()?{kind,error:true}:false;
 }
}
