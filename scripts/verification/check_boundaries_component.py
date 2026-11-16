"""Run in a separate banking-agent Python process; never patches the running server.

P11 uses actual Memory/MCP/ABS and injected client failures. P09 captures the real
client payload at HTTP dispatch, without sending synthetic secrets to providers.
"""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx

from services.banking_agent.app import skills
from services.banking_agent.graph import IntentDecision
from services.common.tracing import trace_value


async def main():
    out = {
        "scope": "Component integration; routing/UI bypassed; fault and egress interception explicitly injected.",  # noqa: E501 -- preserve original probe text
        "checks": [],
    }
    async with httpx.AsyncClient(timeout=20) as client:

        async def login(name):
            r = await client.post(
                "http://keycloak:8080/realms/agentic-banking/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "bank-ui",
                    "username": name,
                    "password": "banking-demo",
                },
            )
            r.raise_for_status()
            return r.json()["access_token"]

        ta, tb = await login("ivan.petrov"), await login("anna.sokolova")
    a = next(
        r for r in await skills.mcp.call("get_customer_accounts", {}, ta) if r["currency"] == "RUB"
    )
    b = next(
        r for r in await skills.mcp.call("get_customer_accounts", {}, tb) if r["currency"] == "RUB"
    )

    async def snapshot():
        return {
            "A": await skills.mcp.call("get_customer_accounts", {}, ta),
            "B": await skills.mcp.call("get_customer_accounts", {}, tb),
        }

    def balances(s):
        return {r["id"]: Decimal(str(r["balance"])) for rows in s.values() for r in rows}

    initial = await snapshot()
    state = {
        "user_id": "customer-001",
        "access_token": ta,
        "conversation_id": "verify-fault-" + uuid.uuid4().hex,
        "message": "Подтверждаю тестовый перевод",
    }
    prepared = await skills.prepare_action(
        {
            **state,
            "action": {
                "type": "transfer",
                "parameters": {
                    "from_account": a["id"],
                    "to_account": b["number"],
                    "amount": "1.00",
                    "description": "Verification fault recovery",
                },
            },
        }
    )
    aid = prepared["confirmation_id"]
    state["confirm_action_id"] = aid
    out["prepared"] = prepared

    async def fail(*args, **kwargs):
        raise httpx.ConnectError("Injected memory client failure")

    async def attempt(name):
        before = await snapshot()
        try:
            result = await skills.confirm_action(state)
            error = None
        except Exception as exc:
            result, error = None, type(exc).__name__ + ": " + str(exc)
        after = await snapshot()
        x, y = balances(before), balances(after)
        row = {
            "case": name,
            "result": result,
            "error": error,
            "before": before,
            "after": after,
            "debit": str(x[a["id"]] - y[a["id"]]),
            "credit": str(y[b["id"]] - x[b["id"]]),
        }
        out["checks"].append(row)
        return row

    with patch.object(skills.memory, "get_action", side_effect=fail):
        await attempt("P11.memory-before-execution")
    with patch.object(skills.memory, "patch_action", side_effect=fail):
        await attempt("P11.memory-after-execution")
    out["pending_after_failure"] = await skills.memory.get_action(aid, "customer-001")
    await attempt("P11.retry-after-failure")
    await attempt("P11.repeat-after-recovery")
    out["action_after_recovery"] = await skills.memory.get_action(aid, "customer-001")
    # Compensate only this run's confirmed debit, using B's own authenticated account.
    final = await snapshot()
    net = balances(initial)[a["id"]] - balances(final)[a["id"]]
    credit = balances(final)[b["id"]] - balances(initial)[b["id"]]
    if net == credit and net > 0:
        out["compensation"] = await skills.mcp.call(
            "create_transfer",
            {
                "from_account": b["id"],
                "to_account": a["number"],
                "amount": str(net),
                "description": "Compensation of verification fault test",
                "idempotency_key": "verify-comp-" + aid,
            },
            tb,
        )
    out["balances_restored"] = balances(initial) == balances(await snapshot())

    marker = "SYNTHETIC_PRIVATE_" + uuid.uuid4().hex[:10]
    out["marker"] = marker
    captured = []

    async def capture_post(client, url, **kwargs):
        captured.append({"url": str(url), "json": kwargs.get("json")})
        raise httpx.ConnectError("Captured at outbound dispatch; no network request sent")

    # Prime the service credential before capturing payloads, so token requests are excluded.
    await skills.tokens.get()
    supplied = {
        "request_type": "destination_info",
        "destination": "Казань",
        "extra": {"private_note": marker},
        "user_query": marker,
    }
    request = await skills._trip_request(
        {"message": "Поездка " + marker, "travel": supplied, "user_id": "customer-001"}
    )
    with patch.object(httpx.AsyncClient, "post", capture_post):
        try:
            await skills.travel.plan(request, "verify-egress-" + uuid.uuid4().hex)
        except httpx.ConnectError:
            pass
        await skills._investment_news("Новости " + marker)
    out["outbound_http_dispatch"] = captured

    llm_requests = []

    async def capture_completion(**kwargs):
        llm_requests.append(kwargs)
        raise RuntimeError("Captured before provider request")

    with patch.object(skills.llm.client.chat.completions, "create", side_effect=capture_completion):
        try:
            await skills.llm.generate_json(
                IntentDecision,
                operation="verification-egress",
                instructions="Classify the request.",
                payload={
                    "user_message": "Поездка " + marker,
                    "nested": {"private_note": marker, "access_token": "SYNTHETIC_TOKEN"},
                },
            )
        except RuntimeError:
            pass
    # Synthetic input only: preserve markers here to assess actual outbound serialization.
    out["llm_dispatch_synthetic_only"] = llm_requests
    out["trace_serialization"] = trace_value(
        {
            "message": "Личная фраза " + marker,
            "nested": {"private_note": marker, "access_token": "SYNTHETIC_TOKEN"},
            "encoded": json.dumps({"password": "SYNTHETIC_PASSWORD", "private_note": marker}),
        }
    )
    print("__EVIDENCE__" + json.dumps(out, ensure_ascii=False, default=str), flush=True)
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        pass


asyncio.run(main())
