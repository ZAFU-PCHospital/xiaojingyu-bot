"""Public repair-activity queue data and user-approved message formatting.

This module is read-only: it neither schedules nor sends messages. The caller
must supply an activity ID obtained from the public activity API or URL.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID


CHINA_TIME = timezone(timedelta(hours=8))
ORIGIN = "https://pczafu.cn"


def activity_url(activity_id: str) -> str:
    try:
        value = UUID(str(activity_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("invalid activity id") from exc
    return f"{ORIGIN}/repair-activities/{value}"


def queue_url(activity_id: str) -> str:
    return activity_url(activity_id).replace("/repair-activities/", "/api/v1/repair-activities/") + "/queue"


@dataclass(frozen=True)
class QueueObservation:
    waiting_count: int | None
    observed_at: datetime


def parse_observation(status: int, body: object) -> QueueObservation:
    if status != 200 or not isinstance(body, dict) or body.get("success") is not True:
        raise ValueError("queue response unavailable")
    data = body.get("data")
    if not isinstance(data, dict) or "waitingCount" not in data:
        raise ValueError("queue data missing")
    count = data["waitingCount"]
    if count is not None and (type(count) is not int or count < 0):
        raise ValueError("invalid waiting count")
    observed_at = data.get("observedAt")
    if not isinstance(observed_at, str):
        raise ValueError("missing observed time")
    try:
        when = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid observed time") from exc
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("observed time lacks time zone")
    return QueueObservation(count, when)


def notice(activity_id: str, observation: QueueObservation) -> str:
    if observation.waiting_count is None:
        raise ValueError("check-in is not open")
    observed = observation.observed_at.astimezone(CHINA_TIME).strftime("%H:%M")
    return (
        "【电脑医院排队情况】\n"
        f"截至 {observed}，当前已有 {observation.waiting_count} 位同学正在排队等待接待。\n"
        f"查看最新情况：{activity_url(activity_id)}\n"
        "人数仅供参考，请按活动通知时间到场。"
    )


def broadcast_notice(observation: QueueObservation) -> str:
    """User-approved concise group broadcast, without an activity link."""
    if observation.waiting_count is None:
        raise ValueError("check-in is not open")
    return (
        f"【活动排队】当前 {observation.waiting_count} 人排队中。"
        "先到先接待，建议错峰到场，减少等候。"
    )
