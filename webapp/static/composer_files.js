export function clipboardFiles(clipboard){
 const direct=[...(clipboard?.files||[])];
 if(direct.length)return direct;
 return [...(clipboard?.items||[])].filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
}
