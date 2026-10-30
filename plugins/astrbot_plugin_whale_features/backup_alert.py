"""Fixed-destination backup alerts. Only allowlisted structured fields cross hosts."""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import uuid
from zoneinfo import ZoneInfo

from aiohttp import web

DESTINATION = os.environ.get('WHALE_BACKUP_GROUP_UMO', '')
HOST = os.environ.get('WHALE_BACKUP_SOURCE_HOST', '')
REASONS = {
    'test': '备份告警联调', 'space': '磁盘可用空间不足',
    'disk_usage': '磁盘使用率超过阈值', 'capacity': '定时备份超过容量上限',
    'database': '数据库备份或校验失败', 'container': '数据库容器不可用',
    'files': '备份文件打包或校验失败', 'prerequisite': '备份所需条件不满足',
    'unexpected': '备份过程发生错误，请查看服务器日志', 'success': '备份成功',
}


class BackupAlertReceiver:
    def __init__(self, plugin):
        self.plugin = plugin

    async def receive(self, request):
        try:
            stored = json.loads((self.plugin.root / 'backup-alert-auth.json').read_text(encoding='utf8'))['sha256']
            authorization = request.headers.get('Authorization','')
            supplied = hashlib.sha256(authorization[7:].encode()).hexdigest() if authorization.startswith('Bearer ') else ''
            if not hmac.compare_digest(stored, supplied):
                return web.json_response({'error':'unauthorized'}, status=401)
        except (OSError, ValueError, KeyError, TypeError):
            return web.json_response({'error':'unauthorized'}, status=401)
        try:
            data = await request.json()
            if not isinstance(data,dict) or set(data) != {'event_id','status','host','finished_at','disk_free_bytes','reason'}:
                raise ValueError('invalid fields')
            event_id = str(uuid.UUID(data['event_id']))
            status, host, reason = data['status'], data['host'], data['reason']
            if status not in ('failure','success') or host != HOST or reason not in REASONS:
                raise ValueError('invalid values')
            if (status == 'success') != (reason == 'success'):
                raise ValueError('inconsistent status')
            value = data['disk_free_bytes']
            if value is not None and (type(value) is not int or not 0 <= value <= 10**18):
                raise ValueError('invalid disk value')
            stamp = dt.datetime.fromisoformat(data['finished_at'].replace('Z','+00:00'))
            if stamp.tzinfo is None or abs((dt.datetime.now(dt.timezone.utc)-stamp).total_seconds()) > 86400:
                raise ValueError('invalid time')
            if not DESTINATION or not HOST or DESTINATION not in self.plugin.managers() or not self.plugin.config.get('notifications_enabled',True):
                return web.json_response({'error':'notification unavailable'},status=503)
            shown_time = stamp.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
            disk = str(value) if value is not None else '未知'
            body = f'【备份{"失败" if status == "failure" else "成功"}】\n主机：{host}\n时间：{shown_time}（北京时间）\n磁盘可用：{disk} 字节'
            if status == 'failure':body += '\n原因：'+REASONS[reason]
            key = 'backup:'+event_id
            old = self.plugin.store.db.execute('SELECT body,state FROM outbox WHERE id=?',(key,)).fetchone()
            if old and old['body'] != body:
                return web.json_response({'error':'event conflict'},status=409)
            if not old:
                self.plugin.store.queue(key,DESTINATION,body)
            claimed = self.plugin.store.db.execute("UPDATE outbox SET state='sending' WHERE id=? AND state='pending'",(key,)).rowcount
            if claimed:
                try:
                    mid = await asyncio.wait_for(self.plugin.send_notice(DESTINATION,body),timeout=8)
                    if not mid:raise RuntimeError('missing delivery receipt')
                except Exception as exc:
                    state = 'rejected' if getattr(exc,'known_rejected',False) else 'unknown'
                    self.plugin.store.db.execute("UPDATE outbox SET state=?,error=? WHERE id=? AND state='sending'",(state,type(exc).__name__,key))
                else:
                    self.plugin.store.db.execute("UPDATE outbox SET state='accepted',message_id=? WHERE id=? AND state='sending'",(str(mid),key))
            row=self.plugin.store.db.execute('SELECT state FROM outbox WHERE id=?',(key,)).fetchone()
            return web.json_response({'state':row['state'] if row else 'unknown'},status=202)
        except (ValueError,TypeError,KeyError,AttributeError):
            return web.json_response({'error':'invalid payload'},status=400)
