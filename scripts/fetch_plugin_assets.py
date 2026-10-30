#!/usr/bin/env python3
"""Fetch pinned upstream plugin assets without storing them in this repository."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir',required=True,type=Path)
    args=ap.parse_args()
    for item in json.loads((ROOT/'config'/'upstream-assets.json').read_text(encoding='utf8')):
        target=args.data_dir.resolve()/'plugins'/item['plugin']/item['path']
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest()!=item['sha256']:
                raise SystemExit('existing asset has a different hash: '+str(target))
            print('verified',item['plugin'],item['path']);continue
        target.parent.mkdir(parents=True,exist_ok=True)
        sources=[{'repository':item['repository'],'commit':item['commit']}]+item.get('mirrors',[])
        fd,tmp=tempfile.mkstemp(prefix='.upstream-',dir=target.parent)
        try:
            os.close(fd)
            valid=False
            for source in sources:
                url=f"https://raw.githubusercontent.com/{source['repository']}/{source['commit']}/{item['path']}"
                try:
                    h=hashlib.sha256()
                    with open(tmp,'wb') as out, urllib.request.urlopen(url,timeout=60) as response:
                        while chunk:=response.read(1024*1024):h.update(chunk);out.write(chunk)
                    if h.hexdigest()==item['sha256']:
                        valid=True;break
                except (OSError,ValueError):
                    continue
            if not valid:
                if item.get('optional'):
                    print('optional asset unavailable:',item['plugin'],item['path']);continue
                raise SystemExit('upstream asset unavailable or checksum mismatch: '+item['plugin']+'/'+item['path'])
            os.replace(tmp,target);print('installed',item['plugin'],item['path'])
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

if __name__=='__main__':main()
