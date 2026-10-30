#!/usr/bin/env python3
"""Create three personas, profiles and group routes on a fresh AstrBot instance."""
import argparse
import copy
import getpass
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
NAMES={'friend':'群友小鲸鱼','customer':'电脑医院客服','management':'管理小鲸鱼'}

def merge(dst,src):
    for key,value in src.items():
        if isinstance(value,dict) and isinstance(dst.get(key),dict):merge(dst[key],value)
        else:dst[key]=copy.deepcopy(value)
    return dst

class Client:
    def __init__(self,base):self.base=base.rstrip('/');self.token='';self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(self,path,method='GET',data=None):
        headers={'Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        req=urllib.request.Request(self.base+path,data=None if data is None else json.dumps(data,ensure_ascii=False).encode('utf8'),headers=headers,method=method)
        with self.opener.open(req,timeout=20) as response:result=json.load(response)
        if result.get('status')!='ok':raise RuntimeError('AstrBot API request failed: '+path)
        return result.get('data')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rendered-dir',required=True,type=Path)
    p.add_argument('--base',default='http://127.0.0.1:6185')
    p.add_argument('--username',help='dashboard username; otherwise prompt interactively')
    p.add_argument('--password-stdin',action='store_true',help='read password from standard input instead of a terminal')
    p.add_argument('--apply',action='store_true',help='create personas, profiles and missing routes')
    a=p.parse_args()
    u=urllib.parse.urlsplit(a.base)
    if u.scheme not in ('http','https') or (u.scheme=='http' and u.hostname not in ('127.0.0.1','localhost','::1')):
        p.error('HTTP is restricted to localhost; use HTTPS for a remote dashboard')
    profiles=json.loads((a.rendered_dir/'profiles.json').read_text(encoding='utf8'))
    whale=json.loads((a.rendered_dir/'astrbot_plugin_whale_features_config.json').read_text(encoding='utf8'))
    personas=json.loads((ROOT/'config'/'personas.json').read_text(encoding='utf8'))
    groups={role:whale[key] for role,key in [('friend','friend_sessions'),('customer','hospital_sessions'),('management','management_sessions')]}
    all_sessions=[s for values in groups.values() for s in values]
    if len(set(all_sessions))!=len(all_sessions) or any('<' in s or ':GroupMessage:' not in s for s in all_sessions):
        p.error('group routes contain placeholders, duplicates or non-group sessions')
    print('Plan: create three personas and profiles, and',len(all_sessions),'group routes.')
    if not a.apply:return
    client=Client(a.base)
    username=a.username or input('AstrBot dashboard username: ')
    password=sys.stdin.readline().rstrip('\r\n') if a.password_stdin else getpass.getpass('AstrBot dashboard password: ')
    client.token=client.call('/api/auth/login','POST',{'username':username,'password':password})['token']
    existing_personas=client.call('/api/v1/personas')
    if any(item['persona_id'] in {v['persona_id'] for v in personas.values()} for item in existing_personas):
        raise SystemExit('A target persona already exists; preserve it and import manually.')
    existing_profiles=client.call('/api/v1/config-profiles')['info_list']
    if any(item['name'] in NAMES.values() for item in existing_profiles):
        raise SystemExit('A target profile already exists; preserve it and import manually.')
    routes=client.call('/api/v1/config-routes')['routing']
    if any(s in routes for s in all_sessions):raise SystemExit('A target session already has a route; preserve it and import manually.')
    default=client.call('/api/v1/config-profiles/default')['config']
    for role in NAMES:client.call('/api/v1/personas','POST',personas[role])
    created={}
    for role,name in NAMES.items():
        result=client.call('/api/v1/config-profiles','POST',{'name':name,'config':merge(copy.deepcopy(default),profiles[role])})
        created[role]=result['conf_id']
    for role,sessions in groups.items():
        for session in sessions:
            client.call('/api/v1/config-routes/'+urllib.parse.quote(session,safe=''),'PUT',{'config_id':created[role]})
    print('Created personas, profiles and group routes.')

if __name__=='__main__':main()
