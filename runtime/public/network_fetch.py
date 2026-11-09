"""Credential-free read-only downloader for explicitly approved public URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('url');parser.add_argument('output')
    args=parser.parse_args();relative=Path(args.output)
    if relative.is_absolute() or '..' in relative.parts:raise SystemExit('output must stay under the current Task directory')
    base=Path(os.environ['VIDEO_USE_STEP_OUTPUT']);target=base/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    request=Request('http://127.0.0.1:18766/public-http',data=json.dumps({'url':args.url}).encode(),
        headers={'Content-Type':'application/json'},method='POST')
    try:
        with urlopen(request,timeout=75) as response:
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:
                temporary=Path(stream.name);digest=hashlib.sha256();size=0
                while chunk:=response.read(64*1024):
                    size+=len(chunk)
                    if size>32*1024**2:raise ValueError('download exceeds32MiB')
                    digest.update(chunk);stream.write(chunk)
            if size==0:raise ValueError('download is empty')
            temporary.chmod(0o600);temporary.replace(target)
    except HTTPError as exc:
        raise SystemExit('approved public HTTPS request failed with HTTP '+str(exc.code)) from None
    finally:
        if 'temporary' in locals():temporary.unlink(missing_ok=True)
    print(json.dumps({'file':str(target),'bytes':size,'sha256':digest.hexdigest()}))


if __name__=='__main__':main()
