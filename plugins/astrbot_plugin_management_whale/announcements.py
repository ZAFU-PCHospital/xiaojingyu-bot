"""Fixed-destination activity notices; never retry an uncertain POST automatically."""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

GROUPS = tuple(x.strip() for x in __import__('os').environ.get('WHALE_ANNOUNCEMENT_GROUP_IDS','').split(',') if x.strip())
ACTIVITY_FIELDS = ('title', 'capacity', 'activityAt', 'signupOpensAt', 'signupClosesAt')


def metadata(value):
    if not isinstance(value, dict) or set(value) != {'location', 'ordinal'}:
        raise ValueError('活动通知需要地点和本学期活动序号；不根据历史记录猜测。')
    location, ordinal = value['location'], value['ordinal']
    if not isinstance(location, str) or not 1 <= len(location.strip()) <= 100 or '\n' in location or '\r' in location:
        raise ValueError('通知地点需为1—100字的单行文本。')
    if type(ordinal) is not int or not 1 <= ordinal <= 999:
        raise ValueError('请提供本学期活动序号（1—999）。')
    return {'location': location.strip(), 'ordinal': ordinal}


def notice(activity, info, url):
    info = metadata(info)
    def date(key):
        value = datetime.fromisoformat(activity[key].replace('Z', '+00:00'))
        if value.utcoffset() is None:
            raise ValueError('活动时间缺少时区，不能发送通知。')
        return value.astimezone(timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M')
    return (
        f"同学们好！电脑医院本学期第{info['ordinal']}次线下志愿维修活动来啦🕒\n"
        f"⏰时间：{date('activityAt')}\n📍地点：{info['location']}\n"
        f"⏰报名开始时间：{date('signupOpensAt')}\n"
        f"名额较少、先到先得，有电脑维修需求的同学点击下方链接填写表单报名👉【{url}】"
        "如果不能正常使用可以复制到浏览器打开\n"
        "后续我们会持续开展志愿维修活动，着急、没抢到名额的同学可以等待下一场！\n"
        "（注意，如有换硅脂需求需自带或在现场联系我们咨询）"
    )


class DeliveryError(Exception):
    def __init__(self, known_rejected=False, status=0, code=None):
        self.known_rejected, self.status, self.code = known_rejected, status, code


class Announcements:
    def __init__(self, db):
        self.db = db
        db.execute('''CREATE TABLE IF NOT EXISTS announcements(
          activity_id TEXT NOT NULL, target TEXT NOT NULL, body TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', message_id TEXT, updated REAL NOT NULL,
          PRIMARY KEY(activity_id,target))''')
        db.execute("UPDATE announcements SET state='unknown' WHERE state='sending'")

    def enqueue(self, activity_id, body):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for group in GROUPS:
                self.db.execute('INSERT OR IGNORE INTO announcements(activity_id,target,body,updated) VALUES(?,?,?,?)',
                                (activity_id, group, body, time.time()))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    async def dispatch(self, activity_id, bindings, send, *, retry=False):
        # Callers have already checked a real inbound confirmation/retry command.
        for group in GROUPS:
            openid = bindings.get(group, '')
            if not re.fullmatch(r'[0-9A-Fa-f]{32}', openid):
                continue
            eligible = ('pending', 'failed') if retry else ('pending',)
            marks = ','.join('?' for _ in eligible)
            row = self.db.execute('SELECT body FROM announcements WHERE activity_id=? AND target=?', (activity_id, group)).fetchone()
            if not row:
                continue
            taken = self.db.execute(f"UPDATE announcements SET state='sending',updated=? WHERE activity_id=? AND target=? AND state IN ({marks})",
                                    (time.time(), activity_id, group, *eligible))
            if not taken.rowcount:
                continue
            try:
                message_id = await send(openid, row['body'])
                if not message_id:
                    raise DeliveryError()
            except asyncio.CancelledError:
                self.finish(activity_id, group, 'unknown')
                raise
            except Exception as exc:
                self.finish(activity_id, group, 'failed' if isinstance(exc, DeliveryError) and exc.known_rejected else 'unknown')
            else:
                self.finish(activity_id, group, 'accepted', str(message_id))
        return self.summary(activity_id)

    def finish(self, activity_id, group, state, message_id=None):
        self.db.execute('UPDATE announcements SET state=?,message_id=?,updated=? WHERE activity_id=? AND target=?',
                        (state, message_id, time.time(), activity_id, group))

    def summary(self, activity_id):
        names = {'pending': '待发送（未绑定或尚未发送）', 'sending': '发送中', 'accepted': 'QQ接口已接收',
                 'failed': '接口明确拒绝，可补发', 'unknown': '结果未知，不自动重发'}
        rows = self.db.execute('SELECT target,state FROM announcements WHERE activity_id=? ORDER BY target', (activity_id,)).fetchall()
        return '\n'.join(f'{r["target"]}：{names[r["state"]]}' for r in rows)


async def send_once(context, platform_id, openid, body):
    """Use existing adapter token with a one-shot HTTP POST, avoiding SDK POST retries."""
    import aiohttp
    platform = context.get_platform_inst(platform_id)
    if platform is None or platform.meta().name != 'qq_official_webhook':
        raise DeliveryError(True)
    # Official Webhook adapter installs BotHttp on Client.http (BotAPI, a
    # different object, uses _http). Access it before issuing any message POST.
    http = platform.client.http
    await http.check_session()
    headers = dict(http._headers)
    host = 'https://sandbox.api.sgroup.qq.com' if http.is_sandbox else 'https://api.sgroup.qq.com'
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post(host + '/v2/groups/' + openid + '/messages',
                                headers=headers, json={'msg_type': 0, 'content': body}, allow_redirects=False) as response:
            raw = await response.content.read(65537)
            if len(raw) > 65536:
                raise DeliveryError()
            if not 200 <= response.status < 300:
                try:
                    error = json.loads(raw)
                    code = error.get('code') if isinstance(error, dict) else None
                except ValueError:
                    code = None
                raise DeliveryError(400 <= response.status < 500 and response.status != 408, response.status, code)
            try:
                result = json.loads(raw)
            except ValueError:
                raise DeliveryError() from None
            if not isinstance(result, dict) or not result.get('id'):
                raise DeliveryError()
            return result['id']
