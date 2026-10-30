#!/usr/bin/env python3
"""Import prepared documents to an existing, empty AstrBot knowledge base."""
import argparse
import getpass
import json
from pathlib import Path
import sys
import time
import urllib.parse
from restore_profiles import Client

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--documents',required=True,type=Path)
    p.add_argument('--kb-id',required=True)
    p.add_argument('--base',default='http://127.0.0.1:6185')
    p.add_argument('--username')
    p.add_argument('--password-stdin',action='store_true')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args();u=urllib.parse.urlsplit(a.base)
    if u.scheme not in ('http','https') or (u.scheme=='http' and u.hostname not in ('127.0.0.1','localhost','::1')):p.error('use localhost HTTP or HTTPS')
    data=json.loads(a.documents.read_text(encoding='utf8'))
    if not isinstance(data.get('documents'),list) or not data['documents']:p.error('no documents')
    print('Plan: import',len(data['documents']),'documents into the specified knowledge base.')
    if not a.apply:return
    client=Client(a.base);username=a.username or input('AstrBot dashboard username: ')
    password=sys.stdin.readline().rstrip('\r\n') if a.password_stdin else getpass.getpass('AstrBot dashboard password: ')
    client.token=client.call('/api/auth/login','POST',{'username':username,'password':password})['token']
    root='/api/v1/knowledge-bases/'+urllib.parse.quote(a.kb_id,safe='')
    known=client.call(root+'/documents?page=1&page_size=100')
    if known.get('total',len(known.get('items',[])))!=0:raise SystemExit('Knowledge base is not empty; preserve it and inspect manually.')
    task=client.call(root+'/documents/import','POST',data);task_id=task['task_id'];print('Import task:',task_id)
    for _ in range(120):
        result=client.call('/api/v1/knowledge-bases/tasks/'+urllib.parse.quote(task_id,safe=''))
        status=result.get('status')
        if status in ('completed','success','failed','error','cancelled'):
            print('Import status:',status)
            if status not in ('completed','success'):raise SystemExit(1)
            detail=result.get('result') or {}
            if detail.get('failed_count',0) or (detail.get('total') is not None and detail.get('success_count') != detail.get('total')):
                raise SystemExit('Import finished with missing or failed documents; inspect task before retry.')
            return
        time.sleep(3)
    raise SystemExit('Task is still running; check its task ID before any retry.')

if __name__=='__main__':main()
