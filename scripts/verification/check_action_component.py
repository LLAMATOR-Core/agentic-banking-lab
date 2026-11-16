"""Run inside banking-agent: actual skill + actual Memory/MCP/ABS, without LLM routing."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from services.banking_agent.app import skills


async def main():
    out = {
        "scope": "Component integration: actual BankingSkills and live services; LLM routing and /v1/chat are bypassed.",  # noqa: E501 -- preserve original probe text
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

        ta = await login("ivan.petrov")
        tb = await login("anna.sokolova")
    arows = await skills.mcp.call("get_customer_accounts", {}, ta)
    brows = await skills.mcp.call("get_customer_accounts", {}, tb)
    a = next(x for x in arows if x["currency"] == "RUB")
    b = next(x for x in brows if x["currency"] == "RUB")

    async def snapshot():
        return {
            "A": await skills.mcp.call("get_customer_accounts", {}, ta),
            "B": await skills.mcp.call("get_customer_accounts", {}, tb),
        }

    params = {
        "from_account": a["id"],
        "to_account": b["number"],
        "amount": "1.00",
        "description": "PDF P06 component",
    }
    base = {
        "user_id": "customer-001",
        "conversation_id": "pdf-p06-" + uuid.uuid4().hex,
        "access_token": ta,
        "message": "Подтверждаю тестовую операцию",
    }

    async def attempt(label, state):
        before = await snapshot()
        try:
            r = await skills.confirm_action(state)
            error = None
        except Exception as exc:
            r = None
            error = type(exc).__name__ + ": " + str(exc)
        after = await snapshot()
        out["checks"].append(
            {"case": label, "before": before, "result": r, "error": error, "after": after}
        )

    prepared = await skills.prepare_action(
        {**base, "action": {"type": "transfer", "parameters": params}}
    )
    out["prepared"] = prepared
    aid = prepared["confirmation_id"]
    await attempt(
        "P06.foreign-owner",
        {**base, "user_id": "customer-002", "access_token": tb, "confirm_action_id": aid},
    )
    await attempt("P06.valid-control", {**base, "confirm_action_id": aid})
    await attempt("P06.already-executed", {**base, "confirm_action_id": aid})
    expired = await skills.memory.create_action(
        user_id=base["user_id"],
        conversation_id=base["conversation_id"],
        action_type="transfer",
        parameters=params,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await attempt("P06.expired", {**base, "confirm_action_id": expired["id"]})
    prepared2 = await skills.prepare_action(
        {**base, "action": {"type": "transfer", "parameters": params}}
    )
    aid2 = prepared2["confirmation_id"]
    out["original_before_tampering"] = prepared2
    await skills.memory.patch_action(
        aid2, base["user_id"], {"parameters": {**params, "amount": "2.00"}}
    )
    await attempt("S05.modified-stored-amount", {**base, "confirm_action_id": aid2})
    print("__EVIDENCE__" + json.dumps(out, ensure_ascii=False, default=str))
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        pass


asyncio.run(main())
