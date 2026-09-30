from __future__ import annotations

import asyncio
from typing import Any

from ddgs import DDGS

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app


def _bounded(value: Any, default: int = 5) -> int:
    return max(1, min(int(value or default), 10))


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

    try:
        results = await asyncio.wait_for(asyncio.to_thread(run), timeout=14)
    except Exception as exc:
        raise ValueError(f"DuckDuckGo news search failed: {exc}") from exc
    return {
        "provider": "duckduckgo",
        "mode": "live",
        "query": query,
        "results": results,
        "content_trust": "untrusted-external",
    }


async def text_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    region = str(arguments.get("region", "ru-ru"))
    limit = _bounded(arguments.get("max_results"))

    def run() -> list[dict[str, str]]:
        rows = DDGS(timeout=10).text(
            query,
            region=region,
            safesearch="moderate",
            max_results=limit,
        )
        return [
            {
                "title": str(item.get("title", "")),
                "body": str(item.get("body", "")),
                "url": str(item.get("href", item.get("url", ""))),
            }
            for item in rows
        ]

    try:
        results = await asyncio.wait_for(asyncio.to_thread(run), timeout=14)
    except Exception as exc:
        raise ValueError(f"DuckDuckGo text search failed: {exc}") from exc
    return {
        "provider": "duckduckgo",
        "mode": "live",
        "query": query,
        "results": results,
        "content_trust": "untrusted-external",
    }


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

app = create_mcp_app("duckduckgo-mcp", tools)
