from __future__ import annotations

import json
from typing import Any

import aiohttp


class AnySearchError(RuntimeError):
    """Raised when AnySearch returns an HTTP or JSON-RPC error."""


class AnySearchClient:
    endpoint = "https://api.anysearch.com/mcp"

    def __init__(self, api_key: str = "", timeout: int = 30) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            try:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    headers=self._headers(),
                ) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise AnySearchError(
                            f"AnySearch HTTP {response.status}: {text[:500]}"
                        )
            except TimeoutError as exc:
                raise AnySearchError("AnySearch request timed out.") from exc
            except aiohttp.ClientError as exc:
                raise AnySearchError(f"AnySearch connection failed: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnySearchError(f"AnySearch returned invalid JSON: {text[:500]}") from exc

        if "error" in data:
            error = data["error"]
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                message = str(error)
            raise AnySearchError(f"AnySearch API error: {message}")

        result = data.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return str(item.get("text", ""))

        return json.dumps(result, ensure_ascii=False, indent=2)

    async def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        freshness: str = "",
        content_types: list[str] | None = None,
    ) -> str:
        arguments: dict[str, Any] = {"query": query}
        if max_results is not None:
            arguments["max_results"] = max_results
        if freshness:
            arguments["freshness"] = freshness
        if content_types:
            arguments["content_types"] = content_types
        return await self._call("search", arguments)

    async def vertical_search(
        self,
        query: str,
        *,
        domain: str,
        sub_domain: str,
        zone: str = "",
        max_results: int | None = None,
        freshness: str = "",
        content_types: list[str] | None = None,
        sub_domain_params: dict[str, Any] | None = None,
    ) -> str:
        arguments: dict[str, Any] = {
            "query": query,
            "domain": domain,
            "sub_domain": sub_domain,
        }
        if zone:
            arguments["zone"] = zone
        if max_results is not None:
            arguments["max_results"] = max_results
        if freshness:
            arguments["freshness"] = freshness
        if content_types:
            arguments["content_types"] = content_types
        if sub_domain_params:
            arguments["sub_domain_params"] = sub_domain_params
        return await self._call("search", arguments)

    async def list_domains(
        self,
        *,
        domain: str = "",
        domains: list[str] | None = None,
    ) -> str:
        if domains:
            return await self._call("list_domains", {"domains": domains})
        return await self._call("list_domains", {"domain": domain})

    async def batch_search(self, queries: list[dict[str, Any]]) -> str:
        return await self._call("batch_search", {"queries": queries})

    async def extract(self, url: str) -> str:
        return await self._call("extract", {"url": url})
