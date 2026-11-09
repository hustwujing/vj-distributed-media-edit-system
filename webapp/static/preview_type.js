const types={mp4:'video/mp4',webm:'video/webm',mov:'video/quicktime',m4v:'video/mp4',mkv:'video/x-matroska',
 mp3:'audio/mpeg',wav:'audio/wav',m4a:'audio/mp4',aac:'audio/aac',ogg:'audio/ogg',flac:'audio/flac',
 png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',webp:'image/webp',gif:'image/gif',svg:'image/svg+xml',avif:'image/avif',
 pdf:'application/pdf',json:'application/json',txt:'text/plain',md:'text/plain',srt:'text/plain',ass:'text/plain',vtt:'text/vtt',csv:'text/csv',py:'text/plain',yaml:'text/plain',yml:'text/plain',log:'text/plain'};
export function previewType(item){
 const supplied=(item.type||item.mime||'').toLowerCase().split(';')[0].trim();
 return supplied&&supplied!=='application/octet-stream'?supplied:types[(item.name||item.file||'').split('.').at(-1).toLowerCase()]||'application/octet-stream';
}
