"""Small durable store. No AstrBot imports; only this plugin's tables."""
import datetime as dt
import hashlib
import ipaddress
import json
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TZ = dt.timezone(dt.timedelta(hours=8))
REPO = 'ZAFU-PCHospital/zafu-pchospital-web'

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def public_url(url):
    p = urlsplit(url.strip().rstrip('。，、！!）)]>'))
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('只读取公开HTTPS链接。')
    host=p.hostname.lower()
    if '.' not in host or host.endswith(('.local','.internal','.localhost')):
        raise ValueError('不读取内网地址。')
    try:
        if not ipaddress.ip_address(host).is_global:raise ValueError('不读取内网地址。')
    except ValueError as exc:
        if '内网' in str(exc):raise
    pairs=parse_qsl(p.query,keep_blank_values=True)
    # Official Heybox public-share entry: resolve its content identifier into
    # the public post URL, without forwarding per-share tracking identifiers.
    if host=='api.xiaoheihe.cn' and p.path=='/v3/bbs/app/api/web/share':
        recognized={'h_camp','h_session_id','h_src','link_id','new_post_share_style_v2'}
        ids=[v for k,v in pairs if k=='link_id']
        if set(k for k,v in pairs)<=recognized and len(ids)==1 and re.fullmatch(r'[A-Za-z0-9_-]{6,64}',ids[0]):
            return 'https://www.xiaoheihe.cn/app/bbs/link/'+ids[0]
    query=[]
    for k,v in pairs:
        if any(x in k.lower() for x in ('token','secret','password','signature','auth','session','key')):
            raise ValueError('带登录或签名参数的链接不自动发送给摘要服务。')
        if not k.lower().startswith(('utm_','spm')):query.append((k,v))
    return urlunsplit(('https',p.netloc.lower(),p.path or '/',urlencode(query),''))

def due_timestamp(value):
    if not value:return None
    x=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if x.tzinfo is None:raise ValueError('截止时间需要时区；中国时间用+08:00。')
    if x.timestamp()<=time.time():raise ValueError('截止时间必须晚于当前时间。')
    return x.timestamp()

def github_notice(event):
    p=event.get('payload') or {}; action=p.get('action'); typ=event.get('type')
    if typ=='IssuesEvent' and action in ('opened','closed'):
        x=p.get('issue') or {}; label='Issue新建' if action=='opened' else 'Issue关闭'
    elif typ=='PullRequestEvent' and action in ('opened','closed','merged'):
        x=p.get('pull_request') or {}
        label='PR开启' if action=='opened' else ('PR合并' if action=='merged' or x.get('merged') or x.get('merged_at') else 'PR关闭（未合并）')
    else:return None
    n=x.get('number') or p.get('number'); title=' '.join(str(x.get('title') or '').split())[:140] or '（标题暂未获取）'
    if not isinstance(n,int) or n<1:return None
    kind='pull' if typ=='PullRequestEvent' else 'issues'
    return f'【GitHub {label}】#{n} {title}\nhttps://github.com/{REPO}/{kind}/{n}'

def validate_deploy(data):
    if not isinstance(data,dict):raise ValueError('JSON必须是对象。')
    allowed={'operation_id','repository','status','commit','previous_commit','duration_seconds','actor','smoke','summary','dry_run'}
    if set(data)-allowed:raise ValueError('包含不支持的字段。不要上传日志、环境变量或凭证。')
    try:uuid.UUID(str(data.get('operation_id','')))
    except ValueError:raise ValueError('operation_id必须是部署尝试的UUID。') from None
    if data.get('repository')!=REPO:raise ValueError('仓库不匹配。')
    if data.get('status') not in ('success','failed','rollback','unknown'):raise ValueError('status无效。')
    for k in ('commit','previous_commit'):
        if data.get(k) and not re.fullmatch(r'[0-9a-fA-F]{7,40}',str(data[k])):raise ValueError('版本必须是Git提交SHA。')
    if data['status'] in ('success','rollback') and (not data.get('commit') or data.get('smoke')!='passed'):
        raise ValueError('成功/回滚成功须提供实际运行版本与smoke=passed；结果不明请用unknown。')
    if data.get('smoke') not in (None,'passed','failed','not_run','unknown'):raise ValueError('smoke无效。')
    for k,limit in [('actor',60),('summary',240)]:
        if k in data:
            if not isinstance(data[k],str) or len(data[k])>limit:raise ValueError(k+'过长或格式错误。')
            if re.search(r'(?i)(sk-[a-z0-9]{12}|bearer\s+\S+|password\s*[:=]|-----BEGIN)',data[k]):raise ValueError('不得提交凭证。')
    duration=data.get('duration_seconds')
    if duration is not None and (isinstance(duration,bool) or not isinstance(duration,(int,float)) or not 0<=duration<=86400):raise ValueError('duration_seconds无效。')
    if 'dry_run' in data and not isinstance(data['dry_run'],bool):raise ValueError('dry_run须为布尔值。')
    return {k:v for k,v in data.items() if k!='dry_run'}

def deployment_text(p):
    names={'success':'成功','failed':'失败','rollback':'回滚成功','unknown':'结果待核实'}
    lines=['【官网部署'+names[p['status']]+'】']
    if p.get('previous_commit'):lines.append('版本：'+p['previous_commit'][:8]+' → '+str(p.get('commit','未知'))[:8])
    elif p.get('commit'):lines.append('版本：'+p['commit'][:8])
    if p.get('duration_seconds') is not None:lines.append('耗时：'+str(p['duration_seconds'])+'秒')
    if p.get('actor'):lines.append('执行人：'+p['actor'])
    if p.get('summary'):lines.append(p['summary'])
    lines.append('记录：'+p['operation_id'][:8])
    return '\n'.join(lines)

class Store:
    def __init__(self,path,muted_github_deploy_sessions=None):
        self.muted_github_deploy_sessions=frozenset(muted_github_deploy_sessions or [])
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT);
        CREATE TABLE IF NOT EXISTS messages(origin TEXT,id TEXT,at REAL,name TEXT,text TEXT,PRIMARY KEY(origin,id));
        CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY,body TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY,origin TEXT,title TEXT,owner TEXT,due REAL,state TEXT,revision INTEGER,created REAL,reminded INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subscriptions(origin TEXT,actor TEXT,appid INTEGER,name TEXT,price INTEGER,released INTEGER,checked REAL,PRIMARY KEY(origin,actor,appid));
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,origin TEXT,body TEXT,state TEXT,created REAL,available REAL,message_id TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY,hash TEXT,payload TEXT,created REAL);
        ''')
        self.db.execute("UPDATE outbox SET state='unknown' WHERE state='sending'")
        for origin in self.muted_github_deploy_sessions:
            self.db.execute("UPDATE outbox SET state='suppressed' WHERE origin=? AND state IN ('pending','rejected') AND (id LIKE 'gh:%' OR id LIKE 'deploy:%')",(origin,))

    def get(self,k,default=None):
        r=self.db.execute('SELECT v FROM kv WHERE k=?',(k,)).fetchone()
        return json.loads(r['v']) if r else default
    def set(self,k,v):self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False)))
    def cache(self,k,body=None,ttl=86400):
        if body is not None:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(k,body,time.time()+ttl));return body
        r=self.db.execute('SELECT body FROM cache WHERE k=? AND expires>?',(k,time.time())).fetchone()
        return r['body'] if r else None
    def observe(self,origin,mid,name,text):
        self.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)',(origin,mid,time.time(),name[:40],text[:1200]))
        self.db.execute('DELETE FROM messages WHERE origin=? AND rowid NOT IN (SELECT rowid FROM messages WHERE origin=? ORDER BY at DESC LIMIT 300)',(origin,origin))
    def recent(self,origin,limit=100):
        rows=self.db.execute('SELECT name,text,at FROM messages WHERE origin=? ORDER BY at DESC LIMIT ?',(origin,limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    def queue(self,key,origin,body,available=None):
        muted=origin in self.muted_github_deploy_sessions and (key.startswith('gh:') or key.startswith('deploy:'))
        state='suppressed' if muted else 'pending'
        self.db.execute('INSERT OR IGNORE INTO outbox(id,origin,body,state,created,available) VALUES(?,?,?,?,?,?)',(key,origin,body[:1800],state,time.time(),available or time.time()))
    def deploy(self,p,origin):
        key=p['operation_id']; h=digest(p)
        old=self.db.execute('SELECT hash FROM deployments WHERE id=?',(key,)).fetchone()
        if old:
            if old['hash']!=h:raise ValueError('同一operation_id不能提交不同结果；后续回滚使用新UUID。')
            r=self.db.execute('SELECT state FROM outbox WHERE id=?',('deploy:'+key,)).fetchone()
            return {'duplicate':True,'delivery':r['state'] if r else 'unknown'}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO deployments VALUES(?,?,?,?)',(key,h,json.dumps(p,ensure_ascii=False),time.time()))
            self.queue('deploy:'+key,origin,deployment_text(p))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
        return {'duplicate':False,'delivery':self.db.execute('SELECT state FROM outbox WHERE id=?',('deploy:'+key,)).fetchone()['state']}

    def todo(self,origin,action,title='',owner='',due='',id=0,expected_revision=0):
        if action=='list':
            return [dict(r) for r in self.db.execute('SELECT * FROM todos WHERE origin=? AND state=\'open\' ORDER BY due IS NULL,due,id LIMIT 30',(origin,))]
        if action=='add':
            if not title.strip() or len(title)>200:raise ValueError('待办标题须为1—200字。')
            if len(owner)>60:raise ValueError('负责人文字过长。')
            when=due_timestamp(due)
            if self.db.execute("SELECT count(*) FROM todos WHERE origin=? AND state='open'",(origin,)).fetchone()[0]>=100:raise ValueError('本群待办已满100项，请先整理。')
            cur=self.db.execute("INSERT INTO todos(origin,title,owner,due,state,revision,created) VALUES(?,?,?,?,'open',1,?)",(origin,title.strip(),owner,when,time.time()))
            return {'id':cur.lastrowid,'revision':1,'saved':True}
        if action not in ('update','done','cancel'):raise ValueError('action无效。')
        row=self.db.execute('SELECT * FROM todos WHERE id=? AND origin=?',(id,origin)).fetchone()
        if not row:raise ValueError('本群没有这条待办。')
        if expected_revision!=row['revision']:raise ValueError('请先读取当前待办和revision再修改。')
        if row['state']!='open':raise ValueError('这条待办已经完成或取消。')
        if action=='update':
            if not title.strip() or len(title)>200 or len(owner)>60:raise ValueError('更新须提供完整标题、负责人和截止时间；不要擅自清空。')
            self.db.execute('UPDATE todos SET title=?,owner=?,due=?,reminded=0,revision=revision+1 WHERE id=?',(title.strip(),owner,due_timestamp(due),id))
        else:self.db.execute('UPDATE todos SET state=?,revision=revision+1 WHERE id=?',(action,id))
        return {'id':id,'state':action,'saved':True}
