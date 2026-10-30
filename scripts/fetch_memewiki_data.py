#!/usr/bin/env python3
"""Fetch the original upstream read-only meme dictionary, verifying its digest."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

COMMIT='386dd63ed889251253576d44066c6de8d476f4f7'
SHA256='05d902ff642dbb894fe2fa61b7623ca9c094cc2e6d26d2f818986296cdacdef3'
URL=f'https://raw.githubusercontent.com/in1t1a1/astrbot_plugin_memewiki/{COMMIT}/data/memes.db'
MIRROR='https://raw.githubusercontent.com/Liujelly/dsh-memewiki/1d3f8266468823fe85290a016eadf3b8d1262de3/data/memes.db'

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir',required=True,type=Path)
    ap.add_argument('--source',type=Path,help='use an already downloaded upstream memes.db')
    args=ap.parse_args()
    target=args.data_dir.resolve()/'plugins'/'astrbot_plugin_memewiki'/'data'/'memes.db'
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest()==SHA256:
            print('dictionary already matches upstream');return
        raise SystemExit('target exists with a different hash; preserve it and decide manually')
    fd,tmp=tempfile.mkstemp(prefix='.memes-',dir=target.parent)
    try:
        h=hashlib.sha256()
        with os.fdopen(fd,'wb') as out:
            if args.source:
                stream=args.source.open('rb')
            else:
                try:stream=urllib.request.urlopen(URL,timeout=60)
                except OSError:stream=urllib.request.urlopen(MIRROR,timeout=60)
            with stream:
                while chunk:=stream.read(1024*1024):h.update(chunk);out.write(chunk)
        if h.hexdigest()!=SHA256:raise SystemExit('dictionary checksum mismatch')
        os.replace(tmp,target);print('upstream dictionary installed and verified')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

if __name__=='__main__':main()
