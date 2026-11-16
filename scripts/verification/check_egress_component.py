"""Capture concrete client dispatch; no test payload reaches external providers."""

import asyncio
import json
import uuid
from unittest.mock import patch

import httpx

from services.banking_agent.app import skills


async def main():
    marker = "PRIVATE_DISPATCH_" + uuid.uuid4().hex[:10]
    captured = []

    async def intercept(client, url, **kwargs):
        data = kwargs.get("json", {})
        captured.append({"url": str(url), "json": data})
        if data.get("method") == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": data["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "probe", "version": "1"},
                    },
                },
                request=httpx.Request("POST", url),
            )
        if data.get("method") == "notifications/initialized":
            return httpx.Response(202, request=httpx.Request("POST", url))
        raise httpx.ConnectError("Dispatch captured; external network request not sent")

    await skills.tokens.get()
    request = await skills._trip_request(
        {
            "message": "Нужна поездка. Личный маркер " + marker,
            "travel": {
                "request_type": "destination_info",
                "destination": "Казань",
                "extra": {"private_note": marker},
            },
            "user_id": "customer-001",
        }
    )
    with patch.object(httpx.AsyncClient, "post", intercept):
        try:
            await skills.travel.plan(request, "verify-dispatch-" + uuid.uuid4().hex)
        except httpx.ConnectError:
            pass
        await skills._investment_news("Личный маркер " + marker + ". Какие новости рынков?")
    print(
        "__EVIDENCE__"
        + json.dumps(
            {
                "scope": "Real client serialization captured at dispatch; MCP handshake responses simulated; remote receipt not tested.",  # noqa: E501 -- preserve original probe text
                "marker": marker,
                "dispatch": captured,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        pass


asyncio.run(main())
