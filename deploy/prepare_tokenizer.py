"""Install a checksummed budget-tokenizer asset during deployment, never inference."""
import hashlib
from pathlib import Path
import tempfile
from urllib.request import urlopen
from webapp.model_token_estimate import data_path,VOCAB_SHA256,VOCAB_URL


def prepare():
    target=data_path()
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==VOCAB_SHA256:return target
    # Reuse a verified download cache when explicitly provided; no secret URLs.
    import os
    cache=Path(os.environ['TIKTOKEN_CACHE_DIR'])/hashlib.sha1(VOCAB_URL.encode()).hexdigest() if os.environ.get('TIKTOKEN_CACHE_DIR') else None
    if cache and cache.exists():content=cache.read_bytes()
    else:
        with urlopen(VOCAB_URL,timeout=30) as response:content=response.read(4*1024**2)
    if hashlib.sha256(content).hexdigest()!=VOCAB_SHA256:raise ValueError('Tokenizer download checksum mismatch')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:
        temporary=Path(stream.name);stream.write(content)
    temporary.chmod(0o644);temporary.replace(target)
    return target


if __name__=='__main__':print(prepare())
