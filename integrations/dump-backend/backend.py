"""Loopback-only, authenticated, single-job dump reader. Never executes an uploaded file."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(os.environ.get('HOSPITAL_DATA','/var/lib/pc-hospital'))
BINARY=Path(os.environ.get('EPHEMERA_BINARY','/opt/pc-hospital/bin/ephemera'))
TRIAGE_BINARY=Path(os.environ.get('TRIAGE_BINARY','/opt/pc-hospital/bin/ntoseye-hospital'))
MAX_FILE=256*1024*1024
MAX_CACHE=512*1024*1024
OUTPUT_LIMIT=14000
JOB_LOCK=threading.Lock()
ALLOWED_SUFFIXES=('qq.com','qpic.cn','weiyun.com','gtimg.com','myqcloud.com')


def validate_url(url):
    parsed=urllib.parse.urlsplit(url)
    host=(parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.port not in (None,80,443):
        raise ValueError('下载地址格式不合法')
    if not any(host==s or host.endswith('.'+s) for s in ALLOWED_SUFFIXES):
        raise ValueError('仅允许NapCat返回的QQ文件域名')
    try: addresses=socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError:raise ValueError('QQ文件域名解析失败') from None
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('文件地址不能指向内网或本机')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url,path):
    validate_url(url)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),SafeRedirect())
    req=urllib.request.Request(url,headers={'User-Agent':'PcHospital-DumpReader/0.1'})
    started=time.monotonic();size=0;digest=hashlib.sha256()
    with opener.open(req,timeout=12) as response,path.open('wb') as output:
        if int(response.headers.get('Content-Length','0') or 0)>MAX_FILE:
            raise ValueError('文件超过256MiB限制')
        while True:
            if time.monotonic()-started>30:raise TimeoutError('文件下载超过30秒')
            block=response.read(128*1024)
            if not block:break
            size+=len(block)
            if size>MAX_FILE:raise ValueError('文件超过256MiB限制')
            output.write(block);digest.update(block)
    if not size:raise ValueError('文件为空')
    return size,digest.hexdigest()


def basic_facts(path):
    with path.open('rb') as f:header=f.read(8192)
    if header[:8]!=b'PAGEDU64' or len(header)<8192:
        return None
    machine=struct.unpack_from('<I',header,0x30)[0]
    if machine!=0x8664:return None
    code=struct.unpack_from('<I',header,0x38)[0]
    params=struct.unpack_from('<4Q',header,0x40)
    return {'format':'Windows kernel dump header (AMD64)','dump_type':struct.unpack_from('<I',header,0xF98)[0],'bugcheck_code':f'0x{code:X}','bugcheck_parameters':[f'0x{x:X}' for x in params]}


def trim_cache():
    cache=ROOT/'cache'
    cache.mkdir(parents=True,exist_ok=True)
    files=[p for folder in (cache,ROOT/'.ntoseye/symbols') for p in folder.rglob('*') if p.is_file() and not p.is_symlink()]
    total=sum(p.stat().st_size for p in files)
    for p in sorted(files,key=lambda p:p.stat().st_mtime):
        if total<=MAX_CACHE:break
        size=p.stat().st_size;p.unlink();total-=size


def clean_parser_output(text):
    text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',text)
    text=re.sub(r'https?://\S+','<symbol-server-url>',text)
    text=re.sub(r'/var/lib/pc-hospital/\S+','<analysis-file>',text)
    return text.strip()


def analyze_triage(path,facts):
    if not TRIAGE_BINARY.is_file():
        return {'status':'partial','analysis_level':'header_only','facts':facts,'reason':'triage_engine_missing','message':'已识别小型内核转储，配套解析器不可用；目前仅有头部字段。'}
    env={key:value for key,value in os.environ.items() if key.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
    env.update(HOME=str(ROOT),NO_COLOR='1',RAYON_NUM_THREADS='2',PC_HOSPITAL_LAZY_TRIAGE='1',PC_HOSPITAL_SYMBOL_TIMEOUT_SECS='10')
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        process=subprocess.Popen([str(TRIAGE_BINARY),'--plain-repl','--dump',str(path)],stdin=subprocess.PIPE,stdout=output,stderr=errors,env=env,start_new_session=True)
        try:
            process.communicate(input=b'!analyze -v\nq\n',timeout=75)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()
            return {'status':'partial','analysis_level':'header_only','facts':facts,'engine':'ntoseye','reason':'analysis_timeout','message':'小型转储分析达到75秒上限，可能缺少可用符号；目前仅确认头部信息。'}
        output.seek(0);text=clean_parser_output(output.read(OUTPUT_LIMIT*8).decode('utf-8','replace'))
        errors.seek(0);error_text=clean_parser_output(errors.read(4000).decode('utf-8','replace'))
        # The module inventory is not a crash explanation; do not send the
        # machine's entire driver list or unreliable unloaded names to the LLM.
        text=text.split('\nloaded modules\n',1)[0].strip()[:OUTPUT_LIMIT]
        if process.returncode or 'bugcheck' not in text.lower():
            print(json.dumps({'engine':'ntoseye','exit_code':process.returncode,'error':error_text[:500]}),flush=True)
            return {'status':'partial','analysis_level':'header_only','facts':facts,'engine':'ntoseye','reason':'parser_failed','message':'小型转储深度解析未完成；目前只确认头部字段，不推断驱动。'}
        frame_indices=[int(x) for x in re.findall(r'^\s*#(\d+)\s+[0-9a-fA-F]{8,16}\s+',text,re.M)]
        frames=max(frame_indices)+1 if frame_indices else 0
        module=re.search(r'bugcheck fault address resolves to ([A-Za-z0-9_.-]+)',text)
        enriched=dict(facts)
        if module:enriched['fault_address_module']=module.group(1)
        # A completed parser invocation is not proof of a complete unwind.
        limited=frames<2 or 'target metadata is still being rebuilt' in text or 'stack unavailable' in text.lower()
        return {'status':'partial' if limited else 'complete','analysis_level':'triage','engine':'ntoseye','parser_completed':True,'facts':enriched,'stack_frames_reported':frames,'stack_limited':limited,'analysis_text':text,'message':'已解析小型转储的实际模块/栈证据。故障地址落入模块仅是线索，不等于已证明根因。'+(' 当前调用栈不完整，不能冒充WinDbg完整诊断。' if limited else ' 工具完成不代表根因已确定。')}


def analyze_local(path):
    facts=basic_facts(path)
    if facts is None:
        return {'status':'unsupported','analysis_level':'none','message':'当前后端未确认支持这种转储格式。首版支持可被Ephemera读取的AMD64 Windows内核dump；普通MDMP、压缩包或损坏文件不能假定兼容。'}
    trim_cache()
    if facts['dump_type']==4:
        return analyze_triage(path,facts)
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        process=subprocess.Popen([str(BINARY),str(path),'--cache',str(ROOT/'cache'),'--','!analyze'],stdout=output,stderr=errors,env={**os.environ,'RUST_LOG':'warn'},start_new_session=True)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait()
            return {'status':'partial','analysis_level':'header_only','facts':facts,'message':'深入分析超过60秒，可能仍需下载/处理符号；目前只读取了转储头部，不能推断缺失堆栈。'}
        output.seek(0);text=output.read(OUTPUT_LIMIT*4).decode('utf-8','replace')
        # Strip terminal escapes. Do not expose stderr paths, URLs, or the download URL.
        text=clean_parser_output(text)[:OUTPUT_LIMIT]
        if process.returncode!=0 or not text:
            errors.seek(0)
            print(json.dumps({'engine':'ephemera','exit_code':process.returncode,'error':clean_parser_output(errors.read(2000).decode('utf-8','replace'))[:500]}),flush=True)
            return {'status':'partial','analysis_level':'header_only','facts':facts,'message':'Ephemera未完成深入分析（可能为格式、缺页或符号问题）。目前仅有头部字段，不是完整诊断。'}
        return {'status':'complete','analysis_level':'ephemera','facts':facts,'analysis_text':text,'message':'工具已完成本次分析；模块/堆栈提示是排查依据，不能单凭名称断言根因。'}


class Handler(BaseHTTPRequestHandler):
    server_version='PcHospital/0.1'
    def log_message(self,*args):pass

    def respond(self,code,data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)

    def do_GET(self):
        if self.path=='/health':
            self.respond(200,{'service':'pc-hospital-dump','engine':'ephemera+ntoseye','binary_present':BINARY.is_file(),'triage_binary_present':TRIAGE_BINARY.is_file(),'single_job':True})
        else:self.respond(404,{'status':'not_found'})

    def do_POST(self):
        if self.path!='/analyze':return self.respond(404,{'status':'not_found'})
        expected='Bearer '+self.server.api_token
        if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
            return self.respond(401,{'status':'unauthorized'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384:raise ValueError('请求大小不合法')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):raise ValueError('请求须为对象')
            if not str(payload.get('filename','')).lower().endswith('.dmp'):raise ValueError('首版仅接受.dmp文件')
            url=payload.get('url')
            if not isinstance(url,str) or len(url)>12000:raise ValueError('缺少有效文件地址')
            validate_url(url)
        except (ValueError,TypeError,OverflowError):
            return self.respond(400,{'status':'rejected','message':'无效文件请求；仅允许QQ文件地址和单个.dmp文件。'})
        if not JOB_LOCK.acquire(blocking=False):
            return self.respond(429,{'status':'busy','message':'已有文件正在分析，请稍后重试。'})
        started=time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix='dump-',dir=ROOT/'jobs') as folder:
                path=Path(folder)/'input.dmp'
                size,digest=download(url,path)
                result=analyze_local(path)
                result.update(size_bytes=size,sha256=digest,elapsed_ms=round((time.monotonic()-started)*1000))
            self.respond(200,result)
        except (ValueError,TimeoutError,urllib.error.URLError,OSError):
            self.respond(400,{'status':'failed','message':'文件获取或分析失败，请重新获取文件链接或稍后重试；未获得完整分析结果。'})
        except Exception:
            self.respond(500,{'status':'failed','message':'解析服务发生错误，未获得完整分析结果。'})
        finally:
            JOB_LOCK.release()


def main():
    token=Path('/etc/pc-hospital/token').read_text().strip()
    if len(token)<32:raise RuntimeError('Backend token missing')
    (ROOT/'jobs').mkdir(parents=True,exist_ok=True)
    (ROOT/'cache').mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',6197),Handler)
    server.api_token=token
    server.serve_forever()


if __name__=='__main__':main()
