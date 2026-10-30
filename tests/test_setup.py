import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class SetupTests(unittest.TestCase):
    def test_render_and_restore_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            tmp=Path(temp);example=json.loads((ROOT/'config'/'group-mapping.example.json').read_text(encoding='utf8'))
            for i,key in enumerate(example,1):
                example[key]=f'{i:032X}' if key.startswith('<GROUP_OPENID') else str(10000000+i)
            mapping=tmp/'mapping.json';mapping.write_text(json.dumps(example),encoding='utf8');out=tmp/'out'
            subprocess.run([sys.executable,str(ROOT/'scripts'/'render_plugin_configs.py'),'--mapping',str(mapping),'--out',str(out)],check=True,capture_output=True)
            data=json.loads((out/'astrbot_plugin_whale_features_config.json').read_text(encoding='utf8'))
            self.assertEqual([len(data[k]) for k in ('friend_sessions','hospital_sessions','management_sessions')],[4,3,2])
            self.assertNotIn('<GROUP_',json.dumps(data))
            subprocess.run([sys.executable,str(ROOT/'scripts'/'restore_profiles.py'),'--rendered-dir',str(out)],check=True,capture_output=True)
            result=subprocess.run([sys.executable,str(ROOT/'scripts'/'render_plugin_configs.py'),'--mapping',str(mapping),'--out',str(out)],capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_restore_profiles_against_local_api(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def respond(self,data):
                raw=json.dumps({'status':'ok','data':data}).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                data={'/api/v1/personas':[], '/api/v1/config-profiles':{'info_list':[]},
                      '/api/v1/config-routes':{'routing':{}}, '/api/v1/config-profiles/default':{'config':{}}}
                self.respond(data[self.path])
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(('POST',self.path))
                self.respond({'token':'synthetic'} if self.path=='/api/auth/login' else {'conf_id':data.get('name','persona')})
            def do_PUT(self):
                self.rfile.read(int(self.headers['Content-Length']));seen.append(('PUT',self.path));self.respond({})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                tmp=Path(temp);example=json.loads((ROOT/'config'/'group-mapping.example.json').read_text(encoding='utf8'))
                for i,key in enumerate(example,1):example[key]=f'{i:032X}' if key.startswith('<GROUP_OPENID') else str(10000000+i)
                mapping=tmp/'mapping.json';mapping.write_text(json.dumps(example),encoding='utf8');out=tmp/'out'
                subprocess.run([sys.executable,str(ROOT/'scripts'/'render_plugin_configs.py'),'--mapping',str(mapping),'--out',str(out)],check=True,capture_output=True)
                result=subprocess.run([sys.executable,str(ROOT/'scripts'/'restore_profiles.py'),'--rendered-dir',str(out),'--base',f'http://127.0.0.1:{server.server_port}','--apply','--username','user','--password-stdin'],input='password\n',text=True,capture_output=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(sum(method=='PUT' for method,_ in seen),9)
            self.assertEqual(sum(path=='/api/v1/personas' for _,path in seen),3)
            self.assertEqual(sum(path=='/api/v1/config-profiles' for _,path in seen),3)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_announcement_targets_are_configurable(self):
        source=ROOT/'plugins'/'astrbot_plugin_management_whale'/'announcements.py'
        with patch.dict(os.environ,{'WHALE_ANNOUNCEMENT_GROUP_IDS':'12345678,87654321'}):
            spec=importlib.util.spec_from_file_location('whale_announcements',source)
            mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        self.assertEqual(mod.GROUPS,('12345678','87654321'))

    def test_case_applicability_annotation_preserves_source(self):
        spec=importlib.util.spec_from_file_location('whale_case_store',ROOT/'plugins'/'astrbot_plugin_pc_hospital'/'case_store.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        row={'case_id':'CASE-000001','body':{'title':'测试','cause':'属该机型通病'},'entry_type':'repair','verification':'已验证','version':1}
        self.assertIn('普遍性尚未由本案例证实',mod.CaseStore.document(row))
        self.assertEqual(row['body']['cause'],'属该机型通病')

    def test_backend_token_not_in_template(self):
        data=json.loads((ROOT/'config'/'templates.json').read_text(encoding='utf8'))
        self.assertEqual(data['plugins']['astrbot_plugin_pc_hospital']['backend_token'],'')

if __name__=='__main__':unittest.main()
