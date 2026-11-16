"""Synthetic MCP transport and telemetry failure probes in a separate process."""

import asyncio
import json
from unittest.mock import patch

import httpx

from services.common.mcp import MCPClient
from services.common.tracing import RecordedObservation, trace_value


async def main():
    out = {
        "scope": "Component-only: MCP HTTP response injected; no remote server or provider contacted. Telemetry span raises locally; durable audit delivery is not tested.",  # noqa: E501 -- preserve original probe text
        "mcp": [],
    }
    client = MCPClient("http://verification.invalid/mcp")
    client.initialized = True
    for label, result, mismatched_id in [
        (
            "valid-control",
            {"structuredContent": [{"id": "synthetic", "balance": "100.00"}], "isError": False},
            False,
        ),
        (
            "wrong-result-type",
            {"structuredContent": "NOT_AN_ACCOUNT_LIST", "isError": False},
            False,
        ),
        (
            "foreign-response-id",
            {"structuredContent": [{"id": "synthetic", "balance": "100.00"}], "isError": False},
            True,
        ),
        (
            "tool-error-control",
            {"content": [{"type": "text", "text": "Synthetic tool rejection"}], "isError": True},
            False,
        ),
    ]:

        async def post(payload, bearer_token, mismatched_id=mismatched_id, result=result):
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "unrelated-id" if mismatched_id else payload["id"],
                    "result": result,
                },
                request=httpx.Request("POST", client.endpoint),
            )

        with patch.object(client, "_post", side_effect=post):
            try:
                returned, error = await client.call("get_customer_accounts", {}), None
            except Exception as exc:
                returned, error = None, type(exc).__name__ + ": " + str(exc)
        out["mcp"].append(
            {
                "case": label,
                "injected_result": result,
                "mismatched_id": mismatched_id,
                "returned": returned,
                "error": error,
            }
        )

    class BrokenSpan:
        calls = 0

        def update(self, **kwargs):
            self.calls += 1
            raise ConnectionError("Injected telemetry transport failure")

    span = BrokenSpan()
    recorded = RecordedObservation(span)
    recorded.update(output={"event": "synthetic-only"})
    out["telemetry_failure"] = {
        "exception_reached_caller": False,
        "span_update_attempts": span.calls,
        "has_output": recorded.has_output,
        "interpretation": "Update failure logged and swallowed; this does not prove a durable audit entry exists.",  # noqa: E501 -- preserve original probe text
    }
    out["redaction"] = trace_value(
        {
            "password": "SYNTHETIC_SECRET",
            "nested": {"access_token": "SYNTHETIC_TOKEN"},
            "encoded": json.dumps({"password": "SYNTHETIC_SECRET"}),
            "free_text": "Пароль: SYNTHETIC_FREE_TEXT_SECRET",
        }
    )
    print("__EVIDENCE__" + json.dumps(out, ensure_ascii=False), flush=True)


asyncio.run(main())
