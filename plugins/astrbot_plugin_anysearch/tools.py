from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent

from .client import AnySearchClient, AnySearchError


CONTENT_TYPES = {
    "web",
    "news",
    "code",
    "doc",
    "academic",
    "data",
    "image",
    "video",
    "audio",
}

FRESHNESS_VALUES = {"day", "week", "month", "year"}


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in text.split(",") if part.strip()]


def _clamp_max_results(value: Any, default: int = 5) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, 100))


def _normalize_content_types(value: Any) -> list[str]:
    items = _split_csv(value)
    return [item for item in items if item in CONTENT_TYPES]


def is_safe_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    )


@dataclass
class AnySearchSearchTool(FunctionTool):
    client: AnySearchClient = field(repr=False, default_factory=AnySearchClient)
    name: str = "anysearch_search"
    description: str = (
        "使用 Anysearch 进行实时联网搜索。适合查询最新事实、新闻、文档、资料检索和一般网页信息。"
        "当用户需要当前资料、网页信息、事实核验、产品信息或资料出处时优先调用本工具。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "必填。搜索关键词或自然语言问题。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "可选。返回结果数量，范围 1-100，默认 5。",
                },
                "freshness": {
                    "type": "string",
                    "description": "可选。时间范围过滤，可填 day、week、month、year。",
                },
                "content_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选。内容类型过滤，可选 web、news、code、doc、academic、data、image、video、audio。",
                },
            },
            "required": ["query"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 5,
        freshness: str = "",
        content_types: list[str] | str | None = None,
    ) -> str:
        _ = event
        if not query.strip():
            return "错误：query 不能为空。"
        freshness = freshness if freshness in FRESHNESS_VALUES else ""
        try:
            return await self.client.search(
                query.strip(),
                max_results=_clamp_max_results(max_results),
                freshness=freshness,
                content_types=_normalize_content_types(content_types),
            )
        except AnySearchError as exc:
            return f"错误：{exc}"


@dataclass
class AnySearchExtractTool(FunctionTool):
    client: AnySearchClient = field(repr=False, default_factory=AnySearchClient)
    name: str = "anysearch_extract"
    description: str = (
        "使用 Anysearch 提取公开网页的可读 Markdown 正文。适合在搜索摘要不够时读取网页内容。"
        "不要用于本地地址、内网地址、私有页面或包含敏感信息的 URL。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "必填。要提取正文的公开 http(s) 网页 URL。",
                }
            },
            "required": ["url"],
        }
    )

    async def run(self, event: AstrMessageEvent, url: str) -> str:
        _ = event
        url = url.strip()
        if not is_safe_http_url(url):
            return "错误：只允许公开的 http(s) URL。"
        try:
            return await self.client.extract(url)
        except AnySearchError as exc:
            return f"错误：{exc}"
