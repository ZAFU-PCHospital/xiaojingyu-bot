#!/usr/bin/env python3
"""Render plugin configuration templates without modifying the template file."""
import argparse
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
PLACEHOLDER=re.compile(r'<(?:BOT_APP_ID|GROUP_OPENID_\d+|GROUP_OPENID|ID_\d+|ID|QQ_ID|OPEN_ID)>')

def render(value,mapping):
    if isinstance(value,dict):return {k:render(v,mapping) for k,v in value.items()}
    if isinstance(value,list):return [render(v,mapping) for v in value]
    if isinstance(value,str):
        return PLACEHOLDER.sub(lambda m:str(mapping.get(m.group(),m.group())),value)
    return value

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mapping',required=True,type=Path,help='private JSON mapping placeholder strings to real values')
    p.add_argument('--out',required=True,type=Path,help='empty destination directory outside this checkout')
    args=p.parse_args()
    mapping=json.loads(args.mapping.read_text(encoding='utf-8-sig'))
    if not isinstance(mapping,dict):p.error('mapping must be an object')
    data=json.loads((ROOT/'config'/'templates.json').read_text(encoding='utf8'))
    required=sorted(set(PLACEHOLDER.findall(json.dumps(data,ensure_ascii=False))))
    for key in required:
        value=mapping.get(key)
        if not isinstance(value,str):p.error('missing string mapping for '+key)
        pattern = r'[0-9A-Fa-f]{32}' if key.startswith('<GROUP_OPENID') else r'\d{5,12}'
        if not re.fullmatch(pattern,value):p.error('invalid mapping for '+key)
    rendered=render(data,mapping)
    unresolved=sorted(set(PLACEHOLDER.findall(json.dumps(rendered,ensure_ascii=False))))
    if unresolved:p.error('unresolved placeholders: '+', '.join(unresolved))
    out=args.out.resolve()
    if out==ROOT or ROOT in out.parents:p.error('output must be outside this checkout')
    if out.exists() and any(out.iterdir()):p.error('destination must be empty')
    out.mkdir(parents=True,exist_ok=True)
    for name,config in rendered['plugins'].items():
        (out/(name+'_config.json')).write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (out/'profiles.json').write_text(json.dumps(rendered['profiles'],ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('rendered',len(rendered['plugins']),'plugin configs and profile reference')

if __name__=='__main__':main()
