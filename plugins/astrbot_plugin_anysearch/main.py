import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .client import AnySearchClient, AnySearchError
from .tools import (
    AnySearchExtractTool,
    AnySearchSearchTool,
    _clamp_max_results,
    _normalize_content_types,
    _split_csv,
    is_safe_http_url,
)


@register(
    "astrbot_plugin_anysearch",
    "AgIzT",
    "Anysearch实时联网搜索插件为 AstrBot 提供免费的原生联网检索能力。它直接调用 Anysearch API，支持通用网页搜索、新闻与文档检索、批量查询和网页正文提取，适合让 Bot 在回答中获取更及时、更可靠、更稳定的外部信息。",
    "0.3",
    "https://github.com/AgIzT/astrbot_plugin_anysearch",
)
class AnySearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = AnySearchClient(api_key=str(self.config.get("api_key", "")))
        self.command_result_chunk_size = self._get_command_result_chunk_size()
        self.chunk_private_command_results = self._get_bool_config(
            "chunk_private_command_results",
            True,
        )
        self.chunk_group_command_results = self._get_bool_config(
            "chunk_group_command_results",
            False,
        )
        self.tools = [
            AnySearchSearchTool(client=self.client),
            AnySearchExtractTool(client=self.client),
        ]
        self.context.add_llm_tools(*self.tools)

    async def terminate(self) -> None:
        for tool in self.tools:
            self.context.provider_manager.llm_tools.remove_func(tool.name)
        logger.info("AnySearch plugin stopped.")

    @filter.command(
        "anysearch",
        alias={"搜索", "websearch"},
        desc="Anysearch实时联网搜索：支持普通搜索、网页提取和批量搜索。",
    )
    async def search_command(
        self,
        event: AstrMessageEvent,
        query: GreedyStr,
    ):
        """Anysearch实时联网搜索：支持普通搜索、网页提取和批量搜索。"""
        query = query.strip()
        if not query:
            yield event.plain_result(self._usage_text())
            return

        parts = query.split()
        action = parts[0].lower() if parts else ""

        if action in {"help", "帮助", "用法"}:
            yield event.plain_result(self._usage_text())
            return

        if action in {"extract", "网页", "提取", "网页提取"}:
            url = " ".join(parts[1:]).strip()
            async for result in self._handle_extract(event, url):
                yield result
            return

        if action in {"batch", "批量", "批量搜索"}:
            queries = query[len(parts[0]) :].strip()
            async for result in self._handle_batch(event, queries):
                yield result
            return

        if is_safe_http_url(query):
            async for result in self._handle_extract(event, query):
                yield result
            return

        try:
            result = await self.client.search(query, max_results=5)
        except AnySearchError as exc:
            yield event.plain_result(f"AnySearch 搜索失败：{exc}")
            return
        for item in self._plain_results(event, result):
            yield item

    def _usage_text(self) -> str:
        return (
            "Anysearch 实时联网搜索用法：\n"
            "/anysearch 关键词\n"
            "/anysearch extract https://example.com/page\n"
            "/anysearch batch 关键词1,关键词2,关键词3\n"
            "中文别名：/搜索 关键词"
        )

    def _get_bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}
        return bool(value)

    def _get_command_result_chunk_size(self) -> int:
        value = self.config.get("command_result_chunk_size", 1200)
        try:
            chunk_size = int(value)
        except (TypeError, ValueError):
            return 1200
        if chunk_size <= 0:
            return 0
        return max(300, min(chunk_size, 1400))

    def _split_plain_text(self, text: str) -> list[str]:
        chunk_size = self.command_result_chunk_size
        if chunk_size <= 0 or len(text) <= chunk_size:
            return [text]

        limit = max(100, chunk_size - 16)
        chunks: list[str] = []
        current = ""

        def flush_current() -> None:
            nonlocal current
            stripped = current.strip()
            if stripped:
                chunks.append(stripped)
            current = ""

        for paragraph in text.split("\n\n"):
            if not paragraph:
                continue

            if len(paragraph) > limit:
                flush_current()
                for line in paragraph.splitlines(keepends=True):
                    while len(line) > limit:
                        segment = line[:limit].strip()
                        if segment:
                            chunks.append(segment)
                        line = line[limit:]
                    if not line.strip():
                        continue
                    if len(current) + len(line) > limit:
                        flush_current()
                    current += line
                continue

            separator = "\n\n" if current else ""
            if len(current) + len(separator) + len(paragraph) > limit:
                flush_current()
            current = f"{current}{separator}{paragraph}" if current else paragraph

        flush_current()
        if len(chunks) <= 1:
            return chunks or [text]
        total = len(chunks)
        return [f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]

    def _should_split_command_result(self, event: AstrMessageEvent, text: str) -> bool:
        if self.command_result_chunk_size <= 0:
            return False
        if len(text) <= self.command_result_chunk_size:
            return False
        if event.get_platform_name() != "aiocqhttp":
            return False
        if event.get_group_id():
            return self.chunk_group_command_results
        return self.chunk_private_command_results

    def _plain_results(self, event: AstrMessageEvent, text: str):
        if not self._should_split_command_result(event, text):
            yield event.plain_result(text)
            return
        for chunk in self._split_plain_text(text):
            yield event.plain_result(chunk)

    async def _handle_extract(
        self,
        event: AstrMessageEvent,
        url: str,
    ):
        url = url.strip()
        if not url:
            yield event.plain_result(
                "用法：/anysearch extract https://example.com/page"
            )
            return
        if not is_safe_http_url(url):
            yield event.plain_result("只允许提取公开的 http(s) 网页地址。")
            return
        try:
            result = await self.client.extract(url)
        except AnySearchError as exc:
            yield event.plain_result(f"AnySearch 提取失败：{exc}")
            return
        for item in self._plain_results(event, result):
            yield item

    async def _handle_batch(
        self,
        event: AstrMessageEvent,
        queries: str,
    ):
        items = _split_csv(queries)
        if not items:
            yield event.plain_result("用法：/anysearch batch 关键词1,关键词2,关键词3")
            return
        query_objects = [
            {"query": item, "max_results": _clamp_max_results(3)}
            for item in items[:5]
        ]
        try:
            result = await self.client.batch_search(query_objects)
        except AnySearchError as exc:
            yield event.plain_result(f"AnySearch 批量搜索失败：{exc}")
            return
        for item in self._plain_results(event, result):
            yield item

    @filter.llm_tool(name="anysearch_batch_search")
    async def anysearch_batch_search(
        self,
        event: AstrMessageEvent,
        queries: str,
        max_results: int = 5,
        freshness: str = "",
        content_types: str = "",
    ):
        """使用 AnySearch 并行执行 1 到 5 个搜索查询。

        Args:
            queries(string): 必填。JSON 数组或用逗号分隔的多个查询。
            max_results(number): 可选。每个查询返回的结果数，1 到 100，默认 5。
            freshness(string): 可选。day、week、month、year 之一。
            content_types(string): 可选。逗号分隔的内容类型，如 web,news,doc。
        """
        _ = event
        try:
            parsed = json.loads(queries)
            if isinstance(parsed, list):
                items = [
                    item if isinstance(item, dict) else {"query": str(item)}
                    for item in parsed
                ]
            else:
                items = [{"query": str(parsed)}]
        except json.JSONDecodeError:
            items = [{"query": item} for item in _split_csv(queries)]

        if not items:
            return "错误：queries 至少需要包含一个查询。"

        normalized = []
        for item in items[:5]:
            query = str(item.get("query", "")).strip()
            if not query:
                continue
            payload = {
                "query": query,
                "max_results": _clamp_max_results(
                    item.get("max_results", max_results)
                ),
            }
            item_freshness = str(item.get("freshness", freshness)).strip()
            if item_freshness in {"day", "week", "month", "year"}:
                payload["freshness"] = item_freshness
            item_content_types = _normalize_content_types(
                item.get("content_types", content_types)
            )
            if item_content_types:
                payload["content_types"] = item_content_types
            normalized.append(payload)

        if not normalized:
            return "错误：queries 需要包含非空查询文本。"

        try:
            result = await self.client.batch_search(normalized)
        except AnySearchError as exc:
            return f"错误：{exc}"
        return result
