#!/usr/bin/env python3
"""Prepare the pinned public hospital documentation for AstrBot import."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True,type=Path,help='checked-out ZAFU-PCHospital-Doc repository')
    p.add_argument('--output',required=True,type=Path,help='private output path outside this repository')
    a=p.parse_args();source=a.source.resolve();out=a.output.resolve()
    if out==ROOT or ROOT in out.parents:p.error('output must be outside this checkout')
    expected=json.loads((ROOT/'config'/'hospital-doc-manifest.json').read_text(encoding='utf8'))
    commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if commit!=expected['commit']:p.error('documentation checkout does not match the pinned commit')
    documents=[]
    for entry in expected['files']:
        rel=entry['path'];path=(source/rel).resolve()
        if source not in path.parents:p.error('invalid documentation path')
        text=path.read_text(encoding='utf8').strip()
        if hashlib.sha256(text.encode()).hexdigest()!=entry['sha256']:p.error('documentation checksum mismatch: '+rel)
        title=next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('#')),path.stem)
        chunks=[]
        for section in re.split(r'(?m)(?=^#{1,4} )',text):
            section=section.strip()
            if not section:continue
            for start in range(0,len(section),700):
                content=section[max(0,start-60):start+760]
                chunks.append(f'【电医技术文档】{title}\n来源：{entry["url"]}\n章节：{rel}\n\n{content}')
        if len(chunks)!=entry['chunks']:p.error('documentation chunk count mismatch: '+rel)
        documents.append({'file_name':'DOC__'+rel.replace('/','__'),'chunks':chunks})
    if out.exists():p.error('output exists; preserve it and choose another path')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({'documents':documents,'batch_size':8,'tasks_limit':1,'max_retries':1},ensure_ascii=False),encoding='utf8')
    print('Prepared',len(documents),'documents and',sum(len(d['chunks']) for d in documents),'chunks.')

if __name__=='__main__':main()
