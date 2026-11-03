/* Public gallery thumbnails share a bounded queue, including after filter changes. */
export class PosterLoader {
 constructor({limit=2,fetcher=(...args)=>fetch(...args),wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),createURL=blob=>URL.createObjectURL(blob),revokeURL=url=>URL.revokeObjectURL(url)}={}){
  this.limit=limit;this.fetcher=fetcher;this.wait=wait;this.createURL=createURL;this.revokeURL=revokeURL;
  this.active=0;this.generation=0;this.pending=[];this.controllers=new Set();this.urls=new Set();this.observer=null;
 }
 show(root){
  this.clear();const generation=this.generation;
  const enqueue=image=>{this.pending.push({image,generation});this.pump();};
  const images=[...root.querySelectorAll('img[data-poster]')];
  if(typeof IntersectionObserver==='undefined'){images.forEach(enqueue);return;}
  this.observer=new IntersectionObserver(entries=>{if(generation!==this.generation)return;for(const entry of entries)if(entry.isIntersecting){this.observer.unobserve(entry.target);enqueue(entry.target);}},{rootMargin:'400px'});
  images.forEach(image=>this.observer.observe(image));
 }
 clear(){this.generation++;this.observer?.disconnect();this.observer=null;this.pending=[];for(const controller of this.controllers)controller.abort();for(const url of this.urls)this.revokeURL(url);this.urls.clear();}
 pump(){while(this.active<this.limit&&this.pending.length){const job=this.pending.shift();if(job.generation!==this.generation||!job.image.isConnected)continue;this.active++;this.load(job);}}
 async load({image,generation}){
  const controller=new AbortController();this.controllers.add(controller);image.dataset.posterState='loading';
  try{
   for(let attempt=0;attempt<3;attempt++){
    if(controller.signal.aborted||generation!==this.generation||!image.isConnected)return;
    const response=await this.fetcher(image.dataset.poster,{credentials:'same-origin',signal:controller.signal});
    if([429,502,503,504].includes(response.status)&&attempt<2){await response.body?.cancel();const retry=Number(response.headers.get('retry-after'));await this.wait(Math.min(5000,Math.max(500,Number.isFinite(retry)?retry*1000:1000)));continue;}
    if(!response.ok)throw Error('Preview unavailable');
    const blob=await response.blob();if(controller.signal.aborted||generation!==this.generation||!image.isConnected)return;
    const url=this.createURL(blob);this.urls.add(url);image.src=url;image.dataset.posterState='loaded';return;
   }
  }catch(error){if(!controller.signal.aborted&&generation===this.generation&&image.isConnected){image.dataset.posterState='failed';image.alt+='（预览暂不可用）';}}
  finally{this.controllers.delete(controller);this.active--;this.pump();}
 }
}
