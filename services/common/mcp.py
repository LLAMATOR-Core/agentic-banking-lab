from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Response
from fastapi.encoders import jsonable_encoder

from services.common.auth import Principal, require_principal
from services.common.tracing import observation

ToolHandler = Callable[[dict[str, Any], Principal], Awaitable[Any]]


@dataclass(slots=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    annotations: dict[str, Any] = field(default_factory=dict)

    def descriptor(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.annotations:
            item["annotations"] = self.annotations
        return item


def create_mcp_app(server_name: str, tools: list[MCPTool]) -> FastAPI:
    """Create a stateless MCP Streamable HTTP server for the core tool methods."""
    app = FastAPI(title=f"{server_name} MCP", version="0.1.0")
    registry = {tool.name: tool for tool in tools}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": server_name}

    @app.post("/mcp", response_model=None)
    async def mcp_endpoint(
        request: dict[str, Any], principal: Principal = Depends(require_principal)
    ) -> dict[str, Any] | Response:
        request_id = request.get("id")
        method = request.get("method")
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": server_name, "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [tool.descriptor() for tool in tools]},
            }
        if method != "tools/call":
            return _mcp_error(request_id, -32601, f"Method {method!r} not found")

        params = request.get("params") or {}
        name = params.get("name")
        tool = registry.get(name)
        if tool is None:
            return _mcp_error(request_id, -32602, f"Unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _mcp_error(request_id, -32602, "Tool arguments must be an object")

        with observation(
            "mcp.tool.call",
            input={"tool": name, "arguments": _redact(arguments)},
            metadata={"server": server_name, "caller": principal.authorized_party},
        ) as span:
            try:
                result = tool.handler(arguments, principal)
                if inspect.isawaitable(result):
                    result = await result
                encoded = jsonable_encoder(result)
                span.update(output={"tool": name, "ok": True})
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": str(encoded)}],
                        "structuredContent": encoded,
                        "isError": False,
                    },
                }
            except (ValueError, KeyError) as exc:
                span.update(output={"tool": name, "ok": False, "error": str(exc)})
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }

    return app


def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _redact(value: dict[str, Any]) -> dict[str, Any]:
    secret_names = {"token", "authorization", "password", "card_number", "cvv"}
    return {
        key: "[REDACTED]" if key.lower() in secret_names else item for key, item in value.items()
    }


class MCPClient:
    def __init__(self, endpoint: str, timeout: float = 15) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    async def list_tools(self, bearer_token: str) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {}, bearer_token)
        return result["tools"]

    async def call(self, name: str, arguments: dict[str, Any], bearer_token: str) -> Any:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, bearer_token
        )
        if result.get("isError"):
            text = (result.get("content") or [{}])[0].get("text", "MCP tool failed")
            raise ValueError(text)
        return result.get("structuredContent")

    async def _request(
        self, method: str, params: dict[str, Any], bearer_token: str
    ) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
            )
            response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise ValueError(body["error"].get("message", "MCP request failed"))
        return body["result"]
