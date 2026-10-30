import datetime
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('portable_backup_hook',str(ROOT/'integrations'/'backup-alert'/'zafu-backup-alert'))
spec=importlib.util.spec_from_loader(loader.name,loader)

@unittest.skipUnless(os.name=='posix','backup hook runs on the Linux website server')
class BackupHookTests(unittest.TestCase):
    def setUp(self):
        self.mod=importlib.util.module_from_spec(spec);loader.exec_module(self.mod)
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.conf=self.root/'backup.conf';self.conf.write_text('STATE_DIR='+str(self.root)+'\nBACKUP_ROOT='+str(self.root)+'\n')
        self.mod.TOKEN_FILE=self.root/'token';self.mod.TOKEN_FILE.write_text('synthetic')
        self.mod.SETTINGS_FILE=self.root/'settings.json';self.mod.SETTINGS_FILE.write_text(json.dumps({'url':'https://example.org/backup'}))
        self.mod.STATE_ROOT=self.root/'rate';self.requests=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"state":"accepted"}'
        class Opener:
            def open(_,req,timeout):self.requests.append(json.loads(req.data));return Response()
        self.opener=Opener()
    def tearDown(self):self.tmp.cleanup()
    def test_safe_fields_and_rate_limit(self):
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        (self.root/'status.json').write_text(json.dumps({'status':'failure','finished_at':now,'disk_free_bytes':1234,'message':'private text'}))
        with patch.dict(os.environ,{'ZAFU_BACKUP_CONFIG':str(self.conf)}),patch.object(self.mod.urllib.request,'build_opener',return_value=self.opener),patch.object(sys,'argv',['hook','failure','可用空间不足：private text']):
            self.assertEqual(self.mod.main(),0);self.assertEqual(self.mod.main(),0)
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0]['disk_free_bytes'],1234)
        self.assertEqual(self.requests[0]['reason'],'space');self.assertNotIn('private text',json.dumps(self.requests))

if __name__=='__main__':unittest.main()
