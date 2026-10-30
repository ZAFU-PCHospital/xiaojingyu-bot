import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock,patch

try:
    import aiohttp  # noqa: F401
    HAS_AIOHTTP=True
except ImportError:HAS_AIOHTTP=False

ROOT=Path(__file__).resolve().parents[1]

class Store:
    def __init__(self):
        self.db=sqlite3.connect(':memory:',isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('create table outbox(id text primary key,origin text,body text,state text,created real,available real,message_id text,error text)')
    def queue(self,key,origin,body):self.db.execute("insert or ignore into outbox(id,origin,body,state) values(?,?,?,'pending')",(key,origin,body))

class Request:
    def __init__(self,payload,token='synthetic'):self.headers={'Authorization':'Bearer '+token};self.payload=payload
    async def json(self):return self.payload

@unittest.skipUnless(HAS_AIOHTTP,'aiohttp is supplied by the AstrBot runtime')
class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        (root/'backup-alert-auth.json').write_text(json.dumps({'sha256':hashlib.sha256(b'synthetic').hexdigest()}))
        with patch.dict(os.environ,{'WHALE_BACKUP_GROUP_UMO':'test:GroupMessage:ABCD','WHALE_BACKUP_SOURCE_HOST':'test-host'}):
            spec=importlib.util.spec_from_file_location('whale_backup_receiver',ROOT/'plugins'/'astrbot_plugin_whale_features'/'backup_alert.py')
            self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.plugin=types.SimpleNamespace(root=root,store=Store(),managers=lambda:[self.mod.DESTINATION],config={},send_notice=AsyncMock(return_value='receipt'))
        self.receiver=self.mod.BackupAlertReceiver(self.plugin)
        import datetime
        self.payload={'event_id':str(uuid.uuid4()),'status':'failure','host':'test-host','finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'disk_free_bytes':1234,'reason':'space'}
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_fixed_destination_and_idempotence(self):
        result=await self.receiver.receive(Request(self.payload));self.assertEqual(json.loads(result.body)['state'],'accepted')
        target,body=self.plugin.send_notice.await_args.args;self.assertEqual(target,self.mod.DESTINATION);self.assertIn('1234',body)
        await self.receiver.receive(Request(self.payload));self.assertEqual(self.plugin.send_notice.await_count,1)
    async def test_auth_and_unknown_fields(self):
        self.assertEqual((await self.receiver.receive(Request(self.payload,'wrong'))).status,401)
        self.assertEqual((await self.receiver.receive(Request(dict(self.payload,message='not allowed')))).status,400)
        self.assertEqual(self.plugin.send_notice.await_count,0)
    async def test_unknown_outcome_is_not_replayed(self):
        self.plugin.send_notice.side_effect=TimeoutError()
        result=await self.receiver.receive(Request(self.payload));self.assertEqual(json.loads(result.body)['state'],'unknown')
        await self.receiver.receive(Request(self.payload));self.assertEqual(self.plugin.send_notice.await_count,1)

if __name__=='__main__':unittest.main()
