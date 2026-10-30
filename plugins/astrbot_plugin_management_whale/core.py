"""Small persistent draft/outcome ledger. Only the inbound QQ handler executes writes."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO = 'ZAFU-PCHospital/zafu-pchospital-web'
LABELS = {'bug', 'enhancement', 'documentation', 'question'}
KINDS = {'issue', 'activity_create', 'activity_update'}
REF_RE = re.compile(r'^([A-F0-9]{8})-V([1-9][0-9]*)$')
COMMAND_RE = re.compile(r'^(确认|取消|查看|核查|补发通知)\s*([A-Fa-f0-9]{8}-[Vv][1-9][0-9]*)\s*$')
UUID_RE = re.compile(r'^[a-fA-F0-9-]{36}$')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse_command(text):
    if text.strip() == '确认':
        return ('确认', '')
    match = COMMAND_RE.fullmatch(text.strip())
    return (match[1], match[2].upper()) if match else None


def validate(kind, payload):
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError('不支持的草稿类型或内容。')
    p = dict(payload)
    announcement = None
    if kind == 'activity_create' and 'announcement' in p:
        from .announcements import metadata
        announcement = metadata(p.pop('announcement'))
    allowed = {
        'issue': {'title', 'body', 'labels'},
        'activity_create': {'title', 'capacity', 'activityAt', 'signupOpensAt', 'signupClosesAt'},
        'activity_update': {'activity_id', 'expected_version', 'title', 'capacity'},
    }[kind]
    if set(p) - allowed:
        raise ValueError('草稿含首版不支持的字段。')
    if kind == 'issue':
        if not isinstance(p.get('title'), str) or not 2 <= len(p['title'].strip()) <= 180:
            raise ValueError('Issue 标题需为2—180字。')
        if not isinstance(p.get('body'), str) or not 10 <= len(p['body']) <= 12000:
            raise ValueError('Issue 正文需为10—12000字。')
        labels = p.get('labels', [])
        if not isinstance(labels, list) or any(not isinstance(x, str) or x not in LABELS for x in labels):
            raise ValueError('标签仅支持bug、enhancement、documentation、question。')
        # Common obvious secrets/contact data: correct before preview, never silently redact a confirmed payload.
        text = p['title'] + '\n' + p['body']
        if re.search(r'-----BEGIN .*PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})|(?<!\d)1[3-9]\d{9}(?!\d)|(?:QQ|qq|电话|手机号)\s*[:：]?\s*\d{5,}', text):
            raise ValueError('草稿疑似含密钥或联系方式，请先脱敏再拟稿。')
        p['labels'] = sorted(set(labels))
    else:
        if kind == 'activity_create' and set(p) != allowed:
            raise ValueError('创建活动需要标题、容量、活动时间、报名开始和截止时间五项。')
        if kind == 'activity_update':
            if not isinstance(p.get('activity_id'), str) or not UUID_RE.fullmatch(p['activity_id']):
                raise ValueError('请先查询并选定真实活动。')
            if not isinstance(p.get('expected_version'), str) or not p['expected_version']:
                raise ValueError('需要查询返回的活动版本。')
            if not set(p) & {'title', 'capacity'}:
                raise ValueError('修改只支持标题或容量。')
        if 'capacity' in p and (type(p['capacity']) is not int or not 1 <= p['capacity'] <= 10000):
            raise ValueError('容量需为1—10000的整数。')
        if 'title' in p and (not isinstance(p['title'], str) or not 2 <= len(p['title'].strip()) <= 120):
            raise ValueError('活动标题需为2—120字。')
        if kind == 'activity_create':
            dates = []
            for name in ('signupOpensAt', 'signupClosesAt', 'activityAt'):
                if not isinstance(p[name], str):
                    raise ValueError('请使用带时区的ISO时间。')
                try:
                    d = datetime.fromisoformat(p[name].replace('Z', '+00:00'))
                except ValueError:
                    raise ValueError('请使用带时区的ISO时间。') from None
                if d.utcoffset() is None:
                    raise ValueError('时间需明确时区，通常为中国时间+08:00。')
                dates.append(d)
            if not dates[0] < dates[1] <= dates[2]:
                raise ValueError('需满足：报名开始 < 报名截止 ≤ 活动时间。')
    if 'title' in p:
        p['title'] = p['title'].strip()
    if announcement is not None:
        p['announcement'] = announcement
    return p


class Ledger:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, origin TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
            operation_id TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL, confirmed_by TEXT,
            expires REAL NOT NULL, result TEXT, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS history (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, revision INTEGER,
            state TEXT, actor TEXT, detail TEXT, at REAL);
        ''')
        self.db.execute("UPDATE drafts SET state='unknown' WHERE state='executing'")

    def _history(self, d, state, actor, detail=''):
        self.db.execute('INSERT INTO history(id,revision,state,actor,detail,at) VALUES(?,?,?,?,?,?)',
                        (d['id'], d['revision'], state, actor, detail, time.time()))

    def get(self, origin, ref):
        match = REF_RE.fullmatch(ref.upper())
        if not match:
            raise ValueError('请使用草稿消息中的编号，例如1234ABCD-V1。')
        row = self.db.execute('SELECT * FROM drafts WHERE id=? AND origin=?', (match[1], origin)).fetchone()
        if not row:
            raise ValueError('本群找不到该草稿。')
        d = dict(row)
        if d['revision'] != int(match[2]):
            raise ValueError(f"草稿已修改，旧编号失效；请查看 {d['id']}-V{d['revision']}。")
        d['payload'] = json.loads(d['payload'])
        d['result'] = json.loads(d['result']) if d['result'] else None
        d['ref'] = f"{d['id']}-V{d['revision']}"
        return d

    def save(self, origin, actor, kind, payload, old_ref=''):
        p = validate(kind, payload)
        now = time.time()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if old_ref:
                old = self.get(origin, old_ref)
                if old['state'] not in {'pending', 'failed', 'conflict'}:
                    raise ValueError('该草稿正在执行、结果未明、已取消或已完成，不能修改重发。')
                if old['kind'] != kind:
                    raise ValueError('修改草稿不能改变操作类型，请另建草稿。')
                did, revision = old['id'], old['revision'] + 1
            else:
                did, revision = secrets.token_hex(4).upper(), 1
            self.db.execute('''INSERT INTO drafts
              (id,origin,revision,kind,payload,digest,state,shown,operation_id,created_by,expires,updated)
              VALUES(?,?,?,?,?,?,'pending',0,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,
              digest=excluded.digest,state='pending',shown=0,operation_id=excluded.operation_id,
              confirmed_by=NULL,result=NULL,expires=excluded.expires,updated=excluded.updated''',
              (did, origin, revision, kind, canonical(p), hashlib.sha256(canonical(p).encode()).hexdigest(),
               str(uuid.uuid4()), actor, now + 86400, now))
            d = self.get(origin, f'{did}-V{revision}')
            self._history(d, 'pending', actor, canonical(p))
            self.db.execute('COMMIT')
            return d
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def mark_shown(self, d):
        self.db.execute('UPDATE drafts SET shown=1 WHERE id=? AND revision=?', (d['id'], d['revision']))

    def confirmation_ref(self, origin):
        """Resolve a bare confirmation only when this group's intended draft is unambiguous."""
        rows = self.db.execute('SELECT id,revision,state,shown,expires,updated FROM drafts WHERE origin=? ORDER BY updated DESC',
                               (origin,)).fetchall()
        pending = [r for r in rows if r['state'] == 'pending' and r['expires'] >= time.time()]
        if not pending:
            raise ValueError('当前没有可确认的草稿：可能已完成、取消或过期，不会重复提交。')
        if len(pending) > 1:
            refs = '、'.join(f"{r['id']}-V{r['revision']}" for r in pending[:6])
            raise ValueError('本群有多份待确认草稿，请先说明要提交哪一份；也可指定“确认 编号”。待选：' + refs)
        row = pending[0]
        if rows[0]['id'] != row['id']:
            raise ValueError('最近一项操作已结束或结果待核查，不会把重复确认应用到更早的草稿。请先选定要提交的草稿。')
        if not row['shown']:
            raise ValueError('草稿尚未完整展示，请先查看草稿再确认。')
        return f"{row['id']}-V{row['revision']}"

    def take(self, origin, ref, actor):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            d = self.get(origin, ref)
            if d['state'] != 'pending':
                raise ValueError('该操作状态为'+d['state']+'，不会重复执行；可发送“查看 '+d['ref']+'”。')
            if not d['shown'] or d['expires'] < time.time():
                raise ValueError('草稿尚未展示或已超过24小时，请重新拟稿。')
            self.db.execute("UPDATE drafts SET state='executing',confirmed_by=?,updated=? WHERE id=?", (actor, time.time(), d['id']))
            self._history(d, 'executing', actor)
            self.db.execute('COMMIT')
            d['confirmed_by'] = actor
            return d
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def finish(self, d, state, result):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE drafts SET state=?,result=?,updated=? WHERE id=? AND revision=?',
                            (state, canonical(result), time.time(), d['id'], d['revision']))
            self._history(d, state, '', canonical(result))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def cancel(self, origin, ref, actor):
        d = self.get(origin, ref)
        if d['state'] != 'pending':
            raise ValueError('该操作已开始或结束，不能通过取消草稿撤销远端操作。')
        self.finish(d, 'cancelled', {'cancelled_by': actor})
        return d


def preview(d):
    p, kind = d['payload'], d['kind']
    header = f"【管理草稿 {d['ref']}】\n"
    if kind == 'issue':
        text = f"仓库：{REPO}\n标题：{p['title']}\n标签：{', '.join(p['labels']) or '无'}\n\n{p['body']}\n\n确认后以上内容将公开到GitHub。"
    elif kind == 'activity_create':
        text = '\n'.join(f'{label}：{p[key]}' for key, label in (
            ('title', '标题'), ('capacity', '容量'), ('activityAt', '活动时间'),
            ('signupOpensAt', '报名开始'), ('signupClosesAt', '报名截止')))
        text += '\n确认后立即公开到官网，不会先保存为网站草稿。'
        if p.get('announcement'):
            from .announcements import GROUPS, notice
            text += '\n\n【创建成功后自动通知】\n目标群：' + '、'.join(GROUPS)
            text += '\n普通通知，不会真正@全体；请群管理员补@全体。\n'
            text += notice(p, p['announcement'], '创建成功后填入真实报名链接')
            text += '\n通知失败不会撤销官网活动；逐群报告结果，结果未知不自动重发。'
    else:
        text = f"活动：{p['activity_id']}\n"
        text += '\n'.join(f'{label}：{p[key]}' for key, label in (('title', '新标题'), ('capacity', '新容量')) if key in p)
        text += '\n确认后直接修改官网；期间数据变化会要求重新确认。'
    return header + text + f"\n\n@我回复：确认\n多份草稿时会先让你选择；也可指定：确认 {d['ref']}\n可要求修改，或发送：取消 {d['ref']}\n草稿24小时有效。"
