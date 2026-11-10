"""Public queue lookup and activity-window notifications for hospital groups."""
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, Plain

from .queue_core import ORIGIN, activity_url, broadcast_notice, notice, parse_observation, queue_url


ALLOWED_SESSIONS = frozenset({
    'default_1905841527:GroupMessage:CC708D9151B7F4AA5D825F99CD9FD6DB',  # test
    'default_1905841527:GroupMessage:EB1BD0DD086D618383E6D72C48CBA495',  # 1071838740
    'default_1905841527:GroupMessage:8B91E482008FE031B61F57D33FCA3111',  # 532502904
    'default_1905841527:GroupMessage:C71A0891377CEACA89F20E39396E12C1',  # 一群
})
INTERVAL = timedelta(minutes=15)
FALLBACK_WINDOW = timedelta(hours=2)
GRACE = timedelta(minutes=2)


def utc_now():
    return datetime.now(timezone.utc)


def activity_start(row):
    try:
        value = datetime.fromisoformat(row['activityAt'].replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def activity_end(row):
    start = activity_start(row)
    if start is None:
        return None
    value = row.get('endsAt')
    if isinstance(value, str):
        try:
            end = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if end.tzinfo is not None and end.utcoffset() is not None:
                return end.astimezone(timezone.utc)
        except ValueError:
            pass
    return start + FALLBACK_WINDOW


def valid_activity(row):
    try:
        activity_url(row['id'])
        return isinstance(row.get('title'), str) and activity_start(row) is not None
    except (KeyError, TypeError, ValueError):
        return False


def due_slot(start, end, now):
    """15-minute slots from activity start through its end; never replay missed slots."""
    if end <= start or now < start + INTERVAL:
        return None
    index = int((now - start) // INTERVAL)
    target = start + index * INTERVAL
    if target <= end and target <= now < target + GRACE:
        return index
    return None


def has_waiting(observation):
    return (not isinstance(observation, str)
            and observation.waiting_count is not None
            and observation.waiting_count > 0)


def candidate_activities(rows, now):
    """Prefer the current activity; do not guess if there are several."""
    items = [r for r in rows if isinstance(r, dict) and valid_activity(r) and r.get('status') != 'ENDED']
    # The website's status, not the broadcast window, defines whether an
    # activity has ended for on-demand questions.
    current = [r for r in items if activity_start(r) <= now]
    if current:
        return current
    future = [r for r in items if activity_start(r) > now]
    if not future:
        return []
    return future


@register('astrbot_plugin_hospital_queue', 'Xiaoyuan', '电脑医院活动排队查询', '0.1.0')
class HospitalQueue(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        root = Path(config.get('data_path', 'data/plugin_data/hospital_queue'))
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / 'broadcasts.sqlite3')
        self.db.execute('''CREATE TABLE IF NOT EXISTS broadcasts (
            activity_id TEXT NOT NULL, start TEXT NOT NULL, slot INTEGER NOT NULL,
            origin TEXT NOT NULL, state TEXT NOT NULL, message_id TEXT, error TEXT,
            PRIMARY KEY(activity_id,start,slot,origin))''')
        self.db.execute("UPDATE broadcasts SET state='unknown',error='interrupted' WHERE state='sending'")
        self.db.commit()
        self.job = None

    def sessions(self):
        return list(dict.fromkeys(str(x) for x in self.config.get('sessions', []) if str(x) in ALLOWED_SESSIONS))

    def allowed(self, event):
        return bool(self.config.get('enabled', False) and event.unified_msg_origin in self.sessions())

    async def initialize(self):
        if self.config.get('enabled', False) and self.config.get('broadcast_enabled', False):
            self.job = asyncio.create_task(self.background())
        logger.info('hospital_queue initialized: enabled=%s broadcast=%s groups=%d',
                    bool(self.config.get('enabled', False)), bool(self.config.get('broadcast_enabled', False)), len(self.sessions()))

    async def terminate(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        self.db.close()

    async def get_json(self, url):
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.content_type != 'application/json':
                    raise ValueError('non-json response')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 512_000:
                        raise ValueError('response too large')
                return response.status, json.loads(raw)

    async def activities(self):
        status, body = await self.get_json(ORIGIN + '/api/v1/repair-activities')
        if status != 200 or not isinstance(body, dict) or body.get('success') is not True or not isinstance(body.get('data'), list):
            raise ValueError('activities unavailable')
        return body['data']

    async def observation(self, activity_id):
        status, body = await self.get_json(queue_url(activity_id))
        if status == 404 and isinstance(body, dict):
            error = body.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            if code in ('ACTIVITY_ENDED', 'ACTIVITY_NOT_FOUND'):
                return code
        result = parse_observation(status, body)
        age = (utc_now() - result.observed_at).total_seconds()
        if age < -60 or age > 180:
            raise ValueError('queue observation stale')
        return result

    @filter.event_message_type(filter.EventMessageType.ALL, priority=900)
    async def direct_queue_question(self, event: AstrMessageEvent):
        """Answer simple, explicit queue questions from the current @ message."""
        if not self.allowed(event):
            return
        parts = event.get_messages()
        if not any(isinstance(p, At) and str(p.qq) == str(event.get_self_id()) for p in parts):
            return
        text = ' '.join(p.text for p in parts if isinstance(p, Plain)).strip()
        if (not text or len(text) > 80 or re.search(r'不查|不要查|不用查|无需查|别查', text)
                or not re.search(r'排队|等候|等待接待', text)
                or not re.search(r'多少|几人|几位|多久|人数|情况|查询|查看|看看', text)):
            return
        # Multiple separate requests remain with the usual conversational agent.
        if re.search(r'[。！？!?；;\n]', text.rstrip('。！？!? \t')):
            return
        match = re.search(r'https?://pczafu\.cn/repair-activities/([0-9a-fA-F-]{36})(?:[/?#\s，,。！？!?；;]|$)', text)
        answer = await self.queue_lookup(event, match.group(1) if match else '')
        logger.info('hospital queue direct lookup handled: origin=%s', event.unified_msg_origin)
        await event.send(event.plain_result(answer + '\n\nAI 建议仅供参考。'))
        event.should_call_llm(False)
        event.stop_event()

    @filter.on_llm_request(priority=1100)
    async def explain_lookup(self, event: AstrMessageEvent, req):
        if not self.allowed(event):
            if getattr(req, 'func_tool', None) is not None:
                import copy
                req.func_tool = copy.copy(req.func_tool)
                req.func_tool.remove_tool('hospital_activity_queue')
            return
        req.extra_user_content_parts.append(TextPart(text=(
            '若客户询问电脑医院活动现场排队人数、何时到场或等待多久，调用 hospital_activity_queue 获取当前公开信息。'
            '仅使用工具返回的活动详情链接和实际人数；人数仅供参考，不估算等待时长、不保证立即接待，提醒按活动通知时间到场。'
            '接口不可用、签到未开放或没有唯一活动时如实说明，绝不将未知说成0或自行编造活动链接。'
        )).mark_as_temp())

    @filter.llm_tool(name='hospital_activity_queue')
    async def queue_lookup(self, event: AstrMessageEvent, activity_id: str = ''):
        """查询本场公开义修活动已签到、等待接待的人数和活动详情链接。

        Args:
            activity_id(string): 已知的活动 UUID；未知时留空，禁止猜测。
        """
        if not self.allowed(event):
            return '仅在已启用的电脑医院客服群提供活动排队查询。'
        try:
            rows = [r for r in await self.activities() if isinstance(r, dict) and valid_activity(r)]
            if activity_id:
                # The ID must correspond to the website's public list.
                url = activity_url(activity_id)
                candidates = [r for r in rows if activity_url(r['id']) == url]
            else:
                candidates = candidate_activities(rows, utc_now())
            if not candidates:
                return '目前没有找到可确认的本场活动，请客户提供活动通知或详情链接。'
            if len(candidates) > 1:
                return '有多场可能的活动，请客户确认具体场次：\n' + '\n'.join(
                    f"{r['title']}：{activity_url(r['id'])}" for r in candidates[:5])
            row = candidates[0]
            url = activity_url(row['id'])
            obs = await self.observation(row['id'])
            if obs in ('ACTIVITY_ENDED', 'ACTIVITY_NOT_FOUND'):
                return '该活动已结束或不存在，请以活动通知为准。'
            if obs.waiting_count is None:
                return f'活动签到尚未开放，等候人数目前不可用。活动页：{url}；请按活动通知时间到场。'
            return notice(row['id'], obs) + '\n以上仅为本次查询结果，不提供等待时间预测。'
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
            logger.info('hospital queue lookup unavailable: %s', type(exc).__name__)
            return '目前无法核实现场等候人数，请查看本场活动通知中的活动页或联系现场工作人员。'

    async def send_one(self, activity_id, start, slot, origin, body):
        # Persist before the non-idempotent POST. Uncertain outcomes never retry.
        key = (activity_id, start.isoformat(), slot, origin)
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO broadcasts(activity_id,start,slot,origin,state) VALUES(?,?,?,?,'sending')", key).rowcount
        self.db.commit()
        if not inserted:
            return
        state, mid, error = 'unknown', None, None
        try:
            from data.plugins.astrbot_plugin_management_whale.announcements import send_once
            platform, kind, openid = origin.split(':', 2)
            if kind != 'GroupMessage' or origin not in ALLOWED_SESSIONS:
                raise ValueError('invalid target')
            mid = await send_once(self.context, platform, openid, body)
            if not mid:
                raise RuntimeError('missing receipt')
            state = 'accepted'
        except asyncio.CancelledError:
            error = 'cancelled'
            raise
        except Exception as exc:
            state = 'rejected' if getattr(exc, 'known_rejected', False) else 'unknown'
            error = json.dumps({'type': type(exc).__name__, 'http_status': getattr(exc, 'status', None),
                                'qq_code': getattr(exc, 'code', None)}, ensure_ascii=False)
            logger.warning('hospital queue broadcast failed: activity=%s slot=%s origin=%s error=%s',
                           activity_id, slot, origin, error)
        finally:
            self.db.execute('UPDATE broadcasts SET state=?,message_id=?,error=? WHERE activity_id=? AND start=? AND slot=? AND origin=?',
                            (state, mid, error, *key))
            self.db.commit()

    async def tick(self, now=None):
        if not self.config.get('enabled', False) or not self.config.get('broadcast_enabled', False):
            return
        now = now or utc_now()
        for row in await self.activities():
            if not isinstance(row, dict) or not valid_activity(row) or row.get('status') == 'ENDED':
                continue
            start = activity_start(row)
            end = activity_end(row)
            slot = due_slot(start, end, now)
            if slot is None:
                continue
            try:
                obs = await self.observation(row['id'])
                if not has_waiting(obs):
                    continue
                body = broadcast_notice(obs)
                await asyncio.gather(*(self.send_one(row['id'], start, slot, origin, body) for origin in self.sessions()))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
                logger.info('hospital queue slot skipped: activity=%s slot=%s reason=%s', row['id'], slot, type(exc).__name__)

    async def background(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('hospital queue loop failed: %s', type(exc).__name__)
            await asyncio.sleep(30)
