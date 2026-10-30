"""Authoritative case catalogue; vector visibility always follows this store."""
import hashlib,json,re,sqlite3,time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit

def scrub(text):
 text=re.sub(r'sk-[A-Za-z0-9_-]{12,}|(?<![0-9a-fA-FxX])[1-9]\d{6,11}(?!\d)','[隐去]',str(text))
 def link(m):
  p=urlsplit(m.group(0))
  if p.query or any((p.hostname or '').endswith(x) for x in ('qq.com','qpic.cn','weiyun.com')):return '[附件/带参数链接已省略]'
  return urlunsplit((p.scheme,p.netloc,p.path,'',''))
 return re.sub(r'https?://[^\s<>]+',link,text)

def canonical_id(value):
 m=re.fullmatch(r'CASE-(\d{1,6})',str(value).upper())
 if not m:raise ValueError('案例编号应为CASE-000001这种形式')
 return f'CASE-{int(m.group(1)):06d}'

def clean_candidate(data):
 if not isinstance(data,dict):raise ValueError('案例应为JSON对象')
 result={}
 for key,limit in {'title':100,'environment':500,'problem':900,'steps':1400,'outcome':700}.items():
  value=data.get(key)
  if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('案例字段缺失或过长：'+key)
  result[key]=scrub(value.strip())
 result['cause']=scrub(str(data.get('cause','尚未证实'))[:600])
 return result

def encode(body):return json.dumps(body,ensure_ascii=False,sort_keys=True)
def fingerprint(body):return hashlib.sha256(encode(body).encode()).hexdigest()
def entry_type(value):return value if value in {'repair','experience','pending'} else 'pending'

class CaseStore:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
  with self.connection() as db:
   db.executescript('''CREATE TABLE IF NOT EXISTS cases (
    n INTEGER PRIMARY KEY AUTOINCREMENT,group_id TEXT NOT NULL,author TEXT NOT NULL,
    body TEXT NOT NULL,verification TEXT NOT NULL,evidence TEXT NOT NULL,evidence_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,
    indexed INTEGER NOT NULL DEFAULT 0,doc_id TEXT,updated REAL NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS source_once ON cases(group_id,evidence_key);
    CREATE TABLE IF NOT EXISTS revisions(n INTEGER,version INTEGER,body TEXT,verification TEXT,author TEXT,updated REAL);
    CREATE TABLE IF NOT EXISTS case_requests(group_id TEXT,message_id TEXT,actor TEXT,action TEXT,state TEXT,result TEXT,updated REAL,
     PRIMARY KEY(group_id,message_id));
    CREATE TABLE IF NOT EXISTS case_focus(group_id TEXT,actor TEXT,case_id TEXT,PRIMARY KEY(group_id,actor));
    CREATE TABLE IF NOT EXISTS revision_details(n INTEGER,version INTEGER,meta TEXT,PRIMARY KEY(n,version));''')
   cols={r['name'] for r in db.execute('PRAGMA table_info(cases)')}
   for key,definition in {'entry_type':"TEXT NOT NULL DEFAULT 'repair'",'index_attempts':'INTEGER NOT NULL DEFAULT 0','index_after':'REAL NOT NULL DEFAULT 0','index_error':"TEXT NOT NULL DEFAULT ''"}.items():
    if key not in cols:db.execute(f'ALTER TABLE cases ADD COLUMN {key} {definition}')
  self.path.chmod(0o600)
 @contextmanager
 def connection(self):
  db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
  try:
   with db:yield db
  finally:db.close()
 @staticmethod
 def decode(row):
  if not row:return None
  x=dict(row);x['case_id']=f'CASE-{x["n"]:06d}';x['body']=json.loads(x['body']);x['evidence']=json.loads(x['evidence']);return x
 def get(self,ref,group=None):
  with self.connection() as db:r=db.execute('SELECT * FROM cases WHERE n=?',(int(canonical_id(ref)[5:]),)).fetchone()
  x=self.decode(r);return x if x and (group is None or x['group_id']==str(group)) else None
 def list(self,group,query='',limit=10):
  with self.connection() as db:
   rows=db.execute('SELECT * FROM cases ORDER BY updated DESC LIMIT 300').fetchall() if group is None else db.execute('SELECT * FROM cases WHERE group_id=? ORDER BY updated DESC LIMIT 300',(str(group),)).fetchall()
  return [self.decode(r) for r in rows if query.casefold() in r['body'].casefold()][:min(limit,100)]
 def lexical(self,group,query,limit=3):
  words=set(re.findall(r'[a-zA-Z0-9_-]{2,}',query.lower()))
  for part in re.findall(r'[\u4e00-\u9fff]{2,}',query):
   words.add(part)
   words.update(part[i:i+2] for i in range(len(part)-1))
  words-={'怎么','如何','电脑','问题','一下','案例','什么','这个'}
  ranked=[]
  for row in self.list(group,limit=100):
   if not row['active']:continue
   title=row['body']['title'].lower();text=encode(row['body']).lower()
   score=sum((4 if w in title else 1) for w in words if w in text)
   if score:ranked.append((score,row))
  return [r for _,r in sorted(ranked,key=lambda x:(x[0],x[1]['updated']),reverse=True)[:limit]]
 def claim(self,group,message_id,actor,action):
  with self.connection() as db:
   db.execute('INSERT OR IGNORE INTO case_requests VALUES(?,?,?,?,?,?,?)',(str(group),str(message_id),str(actor),action,'working','{}',time.time()))
   created=db.execute('SELECT changes()').fetchone()[0]==1
   row=db.execute('SELECT * FROM case_requests WHERE group_id=? AND message_id=?',(str(group),str(message_id))).fetchone()
  return created,dict(row)
 def finish(self,request,result,db=None):
  if request is None:return
  if db is None:
   with self.connection() as tx:self.finish(request,result,tx)
   return
  db.execute("UPDATE case_requests SET state='done',result=?,updated=? WHERE group_id=? AND message_id=?",(encode(result),time.time(),*request))
 def focus(self,group,actor,ref=None,db=None):
  if db is None:
   with self.connection() as tx:return self.focus(group,actor,ref,tx)
  if ref:
   db.execute('INSERT INTO case_focus VALUES(?,?,?) ON CONFLICT(group_id,actor) DO UPDATE SET case_id=excluded.case_id',(str(group),str(actor),ref));return ref
  row=db.execute('SELECT case_id FROM case_focus WHERE group_id=? AND actor=?',(str(group),str(actor))).fetchone()
  return row[0] if row else None
 def create(self,group,actor,body,verification,evidence,kind='pending',request=None):
  fp=fingerprint(body);source_key=hashlib.sha256(encode({'ids':sorted(x['message_id'] for x in evidence),'topic':body['title'].casefold()}).encode()).hexdigest()
  with self.connection() as db:
   old=db.execute('SELECT * FROM cases WHERE group_id=? AND (evidence_key=? OR fingerprint=?)',(str(group),source_key,fp)).fetchone()
   if old:
    row=self.decode(old)
    if row['fingerprint']!=fp:raise ValueError(f'与{row["case_id"]}来源重合但内容有变化，请补充该条，未覆盖旧内容')
    result={'status':'duplicate','case_id':row['case_id'],'active':bool(row['active'])}
    self.finish(request,result,db)
    if row['active']:self.focus(group,actor,row['case_id'],db)
    return row,False
   cur=db.execute('INSERT INTO cases(group_id,author,body,verification,evidence,evidence_key,fingerprint,updated,entry_type) VALUES(?,?,?,?,?,?,?,?,?)',(str(group),str(actor),encode(body),verification,encode(evidence),source_key,fp,time.time(),entry_type(kind)))
   ref=f'CASE-{cur.lastrowid:06d}';self.finish(request,{'status':'saved','case_id':ref},db);self.focus(group,actor,ref,db)
  return self.get(ref,group),True
 def revise(self,ref,group,actor,body,verification,evidence,kind,version,request=None):
  old=self.get(ref,group)
  if not old:raise ValueError('本群没有这个案例')
  with self.connection() as db:
   current=db.execute('SELECT version FROM cases WHERE n=?',(old['n'],)).fetchone()
   if current['version']!=version:raise ValueError('案例刚被更新，请重新读取后再修改')
   self.archive(db,old,actor)
   changed=db.execute('UPDATE cases SET body=?,verification=?,evidence=?,entry_type=?,fingerprint=?,version=version+1,indexed=0,index_attempts=0,index_after=0,index_error=\'\',updated=? WHERE n=? AND version=?',(encode(body),verification,encode(evidence),entry_type(kind),fingerprint(body),time.time(),old['n'],version)).rowcount
   if changed!=1:raise ValueError('案例刚被更新，请重新读取后再修改')
   self.finish(request,{'status':'updated','case_id':old['case_id']},db);self.focus(group,actor,old['case_id'],db)
  return self.get(ref,group)
 @staticmethod
 def archive(db,old,actor):
  db.execute('INSERT INTO revisions(n,version,body,verification,author,updated) VALUES(?,?,?,?,?,?)',(old['n'],old['version'],encode(old['body']),old['verification'],str(actor),time.time()))
  db.execute('INSERT OR IGNORE INTO revision_details VALUES(?,?,?)',(old['n'],old['version'],encode({k:old[k] for k in ('evidence','entry_type','active')})))
 def edit(self,ref,group,actor,action,text='',request=None):
  old=self.get(ref,group)
  if not old:raise ValueError('本群没有这个案例')
  if action not in {'停用','恢复'}:raise ValueError('正文修改需整理后完整更新')
  with self.connection() as db:
   if db.execute('SELECT version FROM cases WHERE n=?',(old['n'],)).fetchone()[0]!=old['version']:raise ValueError('案例刚被更新，请重试')
   self.archive(db,old,actor)
   changed=db.execute('UPDATE cases SET active=?,version=version+1,indexed=0,index_attempts=0,index_after=0,index_error=\'\',updated=? WHERE n=? AND version=?',(int(action=='恢复'),time.time(),old['n'],old['version'])).rowcount
   if changed!=1:raise ValueError('案例刚被更新，请重试')
   self.finish(request,{'status':'disabled' if action=='停用' else 'restored','case_id':old['case_id']},db);self.focus(group,actor,old['case_id'],db)
  return self.get(ref,group)
 def publish(self,ref,version,doc_id):
  with self.connection() as db:return db.execute("UPDATE cases SET indexed=1,doc_id=?,index_error='',index_after=0 WHERE n=? AND version=? AND active=1",(doc_id,int(canonical_id(ref)[5:]),version)).rowcount==1
 def index_failed(self,record,error):
  attempt=record['index_attempts']+1;delay=[30,120,600][min(attempt-1,2)]
  with self.connection() as db:db.execute('UPDATE cases SET index_attempts=?,index_after=?,index_error=? WHERE n=? AND version=? AND active=1 AND indexed=0',(attempt,time.time()+delay,str(error)[:160],record['n'],record['version']))
 def pending(self):
  with self.connection() as db:rows=db.execute('SELECT * FROM cases WHERE active=1 AND indexed=0 AND index_attempts<3 AND index_after<=? ORDER BY updated LIMIT 2',(time.time(),)).fetchall()
  return [self.decode(r) for r in rows]
 def visible_document(self,doc_id,group,shared=False):
  with self.connection() as db:
   row=db.execute('SELECT 1 FROM cases WHERE doc_id=? AND active=1 AND indexed=1',(doc_id,)).fetchone() if shared else db.execute('SELECT 1 FROM cases WHERE doc_id=? AND group_id=? AND active=1 AND indexed=1',(doc_id,str(group))).fetchone()
   return row is not None
 @staticmethod
 def document(record):
  labels={'environment':'设备与环境','problem':'故障现象','steps':'排查与处理（建议与已实施须区分）','cause':'原因及证据边界','outcome':'实际结果','supplement':'管理员补充','correction':'管理员更正'}
  kind={'repair':'维修案例','experience':'经验条目','pending':'待验证案例'}.get(record.get('entry_type'),'维修案例')
  def frame(value):
   return value.replace('通病','所谓通病（记录者说法，普遍性尚未由本案例证实）')
  lines=[f'{record["case_id"]}：{frame(record["body"]["title"])}',f'类别：{kind}；验证状态：{record["verification"]}；版本：{record["version"]}。案例中提到的型号和症状仅属于该案例；单个案例不能证明普遍性，也不能证明适用于当前用户。']
  lines.extend(labels[k]+'：'+frame(v) for k,v in record['body'].items() if k in labels)
  return '\n\n'.join(lines)
