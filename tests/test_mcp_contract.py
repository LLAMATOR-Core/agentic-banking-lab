from typing import Any

from fastapi.testclient import TestClient

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app


async def echo(arguments: dict[str, Any], principal: Principal) -> dict[str, Any]:
    return {"value": arguments["value"], "subject": principal.subject}


def test_mcp_discovery_and_structured_result(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_mcp_app(
        "contract-test",
        [
            MCPTool(
                "echo",
                "Echo a value",
                {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
                echo,
                {"readOnlyHint": True},
            )
        ],
    )
    with TestClient(app) as client:
        listed = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}}
        ).json()
        assert listed["result"]["tools"][0]["name"] == "echo"
        called = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "2",
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "ok"}},
            },
        ).json()
        assert called["result"]["structuredContent"] == {
            "value": "ok",
            "subject": "customer-001",
        }
