// Text revisions are scoped to the fenced dispatch generation, then sequence.
const terminal=s=>['succeeded','failed','cancelled'].includes(s);
export function compareVersion(a=[0,0],b=[0,0]){return (a[0]-b[0])||(a[1]-b[1]);}
export function mergeTurn(current,incoming){
 if(!current)return incoming;
 incoming={...incoming,_lastEventAt:current._lastEventAt,stream_transport:incoming.stream_transport??current.stream_transport};
 if(incoming.updated<current.updated)return current;
 if(incoming._text_unchanged||(!terminal(incoming.status)&&compareVersion(incoming.text_version,current.text_version)<0))
  return {...incoming,text:current.text,text_version:current.text_version,text_phase:current.text_phase,_lastTextAt:current._lastTextAt};
 return {...incoming,_lastTextAt:current._lastTextAt};
}
export function applyText(current,payload,snapshot=false){
 if(!current||terminal(current.status))return current;
 const version=payload.text_version||[0,0],previous=current.text_version||[0,0];
 if(compareVersion(version,previous)<=0&&version[0])return current;
 if(!snapshot&&payload.base_version&&compareVersion(payload.base_version,previous)!==0)return {...current,_streamGap:true};
 return {...current,text:snapshot?payload.text:(current.text||'')+(payload.delta||''),text_version:version,text_phase:payload.text_phase||'streaming',_lastTextAt:Date.now(),_streamGap:false};
}
export function isTyping(turn,now=Date.now()){return !terminal(turn.status)&&turn.text_phase==='streaming'&&now-(turn._lastTextAt||0)<1800;}
