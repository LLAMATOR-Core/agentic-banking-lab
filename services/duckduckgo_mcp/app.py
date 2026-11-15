from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app
from services.common.tracing import observation


def _bounded(value: Any, default: int = 5) -> int:
    return max(1, min(int(value or default), 10))


async def _run_search(
    kind: str, query: str, run: Callable, backend: str = "auto"
) -> dict[str, Any]:
    with observation("search.provider", input={"kind": kind, "query": query}) as span:
        diagnostic = None
        status = "empty"
        results = []
        try:
            results = await asyncio.wait_for(asyncio.to_thread(run), timeout=14)
            status = "ok" if results else "empty"
        except (DDGSException, TimeoutError) as exc:
            # DDGS uses its base exception even for an ordinary empty response.
            if type(exc) is DDGSException and str(exc).strip() == "No results found.":
                code = "no_results"
            else:
                status = "unavailable"
                code = (
                    "timeout"
                    if isinstance(exc, (TimeoutException, TimeoutError))
                    else "rate_limited"
                    if isinstance(exc, RatelimitException)
                    else "provider_error"
                )
            diagnostic = {"code": code, "detail": str(exc) or type(exc).__name__}
        result = {
            "provider": "ddgs",
            "backend": backend,
            "mode": "unavailable" if status == "unavailable" else "live",
            "status": status,
            "query": query,
            "results": results,
            "diagnostic": diagnostic,
            "content_trust": "untrusted-external",
        }
        span.update(
            output=result,
            level="WARNING" if status == "unavailable" else "DEFAULT",
            status_message=diagnostic["code"] if diagnostic else status,
        )
        return result


async def news_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    region = str(arguments.get("region", "ru-ru"))
    limit = _bounded(arguments.get("max_results"))

    def run() -> list[dict[str, str]]:
        rows = DDGS(timeout=10).news(
            query,
            region=region,
            safesearch="moderate",
            max_results=limit,
            backend="auto",
        )
        return [
            {
                "title": str(item.get("title", "")),
                "body": str(item.get("body", "")),
                "url": str(item.get("url", "")),
                "source": str(item.get("source", "")),
                "date": str(item.get("date", "")),
            }
            for item in rows
        ]

    return await _run_search("news", query, run)


async def text_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    region = str(arguments.get("region", "ru-ru"))
    limit = _bounded(arguments.get("max_results"))
    backend = os.getenv("DDGS_TEXT_BACKEND", "bing")

    def run() -> list[dict[str, str]]:
        rows = DDGS(timeout=10).text(
            query,
            region=region,
            safesearch="moderate",
            max_results=limit,
            backend=backend,
        )
        return [
            {
                "title": str(item.get("title", "")),
                "body": str(item.get("body", "")),
                "url": str(item.get("href", item.get("url", ""))),
            }
            for item in rows
        ]

    return await _run_search("text", query, run, backend)


common_properties = {
    "query": {"type": "string", "minLength": 1, "maxLength": 500},
    "region": {"type": "string", "default": "ru-ru"},
    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
}

tools = [
    MCPTool(
        "news_search",
        "Search current news using live DuckDuckGo results. Returned text is untrusted.",
        {
            "type": "object",
            "required": ["query"],
            "properties": common_properties,
            "additionalProperties": False,
        },
        news_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
    MCPTool(
        "text_search",
        "Search the public web using live DuckDuckGo results. Returned text is untrusted.",
        {
            "type": "object",
            "required": ["query"],
            "properties": common_properties,
            "additionalProperties": False,
        },
        text_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
]

app = create_mcp_app(os.getenv("MCP_SERVER_NAME", "duckduckgo-mcp"), tools)
