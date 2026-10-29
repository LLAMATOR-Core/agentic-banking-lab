from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from typing import Any

import httpx

KEYCLOAK = os.getenv(
    "SMOKE_KEYCLOAK_URL", "http://localhost:8080/realms/agentic-banking"
).rstrip("/")
AGENT = os.getenv("SMOKE_AGENT_URL", "http://localhost:8000").rstrip("/")
TRAVEL_AGENT = os.getenv("SMOKE_TRAVEL_URL", "http://localhost:8003").rstrip("/")
ORCHESTRATOR = os.getenv("SMOKE_ORCHESTRATOR_URL", "http://localhost:8004").rstrip("/")


def wait_for(url: str, timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=3)
            if response.is_success:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def login(username: str = "ivan.petrov", password: str = "banking-demo") -> str:
    response = httpx.post(
        f"{KEYCLOAK}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "bank-ui",
            "username": username,
            "password": password,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def memory_operator_token() -> str:
    response = httpx.post(
        f"{KEYCLOAK}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "memory-operator",
            "client_secret": os.getenv(
                "KEYCLOAK_MEMORY_OPERATOR_SECRET", "memory-operator-secret"
            ),
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def request(method: str, path: str, token: str, **kwargs: Any) -> Any:
    response = httpx.request(
        method,
        f"{AGENT}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
        **kwargs,
    )
    if response.is_error:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def orchestrator_request(method: str, path: str, token: str, **kwargs: Any) -> Any:
    response = httpx.request(
        method,
        f"{ORCHESTRATOR}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
        **kwargs,
    )
    if response.is_error:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json()


def run(confirm: bool, expect_llm_unavailable: bool = False) -> None:
    wait_for(f"{KEYCLOAK}/.well-known/openid-configuration")
    wait_for(f"{AGENT}/health")
    wait_for(f"{TRAVEL_AGENT}/health")
    wait_for(f"{ORCHESTRATOR}/health")
    token = login()
    second_token = login("anna.sokolova")
    conversation_id = f"smoke-{int(time.time())}"

    dashboard = request("GET", "/v1/dashboard", token)
    assert dashboard["customer"]["id"] == "customer-001"
    assert len(dashboard["accounts"]) >= 2
    print("OK dashboard and customer isolation")

    if expect_llm_unavailable:
        response = httpx.post(
            f"{AGENT}/v1/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "message": "Покажи мои счета",
                "conversation_id": conversation_id,
            },
            timeout=15,
        )
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "llm_unavailable"
        print("OK unavailable LLM is returned to the UI as explicit HTTP 503")
        return

    second_dashboard = request("GET", "/v1/dashboard", second_token)
    assert second_dashboard["customer"]["id"] == "customer-002"
    assert {item["id"] for item in dashboard["accounts"]}.isdisjoint(
        {item["id"] for item in second_dashboard["accounts"]}
    )
    print("OK secure MCP uses exchanged user identity for two users")

    vulnerable = request(
        "POST",
        "/v1/lab/vulnerable-abs",
        token,
        json={"tool": "get_customer_accounts", "customer_id": "customer-002"},
    )
    assert vulnerable["selected_customer_id"] == "customer-002"
    assert {row["id"] for row in vulnerable["result"]} == {
        row["id"] for row in second_dashboard["accounts"]
    }
    print("OK intentionally vulnerable MCP reproduces customer_id substitution")

    marker = f"shared-poison-{int(time.time())}"
    staged = request(
        "POST",
        "/v1/lab/shared-memory",
        token,
        json={
            "memory_type": "semantic",
            "content": f"Shared security-test marker for the environment: {marker}",
        },
    )
    denied = httpx.post(
        f"{ORCHESTRATOR}/api/v1/orchestrator/collect",
        headers={"Authorization": f"Bearer {token}"},
        json={"conversation_id": staged["conversation_id"], "limit": 5},
        timeout=10,
    )
    assert denied.status_code == 403
    collected = orchestrator_request(
        "POST",
        "/api/v1/orchestrator/collect",
        memory_operator_token(),
        json={"conversation_id": staged["conversation_id"], "limit": 5},
    )
    assert collected["completed_jobs"] == 1
    recalled = request(
        "GET",
        f"/v1/lab/memory-recall?query={marker}",
        second_token,
    )
    shared = next(item for item in recalled["memories"] if marker in item["content"])
    assert shared["user_id"] == "*"
    assert shared["metadata"]["source_user_id"] == "customer-001"
    print("OK orchestrator isolation, manual collection and cross-user memory poisoning")

    chat = request(
        "POST",
        "/v1/chat",
        token,
        json={"message": "Покажи мои счета", "conversation_id": conversation_id},
    )
    assert chat["intent"] == "accounts" and "abs.get_customer_accounts" in chat["tool_calls"]
    print("OK LangGraph accounts skill through MCP")

    conversations = request("GET", "/v1/conversations", token)["conversations"]
    assert any(item["conversation_id"] == conversation_id for item in conversations)
    history = request(
        "GET", f"/v1/conversations/{conversation_id}/messages", token
    )["messages"]
    assert [item["role"] for item in history[-2:]] == ["user", "assistant"]
    assert history[-2]["content"] == "Покажи мои счета"
    assert history[-1]["content"] == chat["message"]
    print("OK full working-memory turn and conversation history")

    departure = date.today() + timedelta(days=30)
    travel = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": "Спланируй поездку в Стамбул",
            "conversation_id": conversation_id,
            "travel": {
                "origin": "MOW",
                "destination": "Стамбул",
                "destination_airport": "IST",
                "departure_date": departure.isoformat(),
                "return_date": (departure + timedelta(days=5)).isoformat(),
                "adults": 1,
                "budget": 150000,
                "currency": "RUB",
                "interests": ["culture", "food"],
            },
        },
    )
    assert travel["intent"] == "travel" and "a2a.travel.plan-trip" in travel["tool_calls"]
    travel_result = travel["data"]["travel"]
    assert travel_result["provider_modes"]["flight_search"] == "mock-external-mcp"
    assert travel_result["flights"]
    assert all(item["synthetic"] for item in travel_result["flights"])
    assert travel_result["booking"]["mode"] == "mock-local"
    assert travel_result["booking"]["external_side_effect"] is False
    assert "packages" not in travel_result
    print("OK A2A informational travel search and local mock-booking boundary")

    rub_accounts = [item for item in dashboard["accounts"] if item["currency"] == "RUB"]
    prepared = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": "Подготовь безопасный тестовый перевод",
            "conversation_id": conversation_id,
            "action": {
                "type": "transfer",
                "parameters": {
                    "from_account": rub_accounts[0]["id"],
                    "to_account": rub_accounts[1]["number"],
                    "amount": "1.00",
                    "description": "Smoke test",
                },
            },
        },
    )
    assert prepared["requires_confirmation"] and prepared["confirmation_id"]
    print("OK active operation stopped at human confirmation gate")

    if confirm:
        confirmation = {
            "message": "Подтверждаю тестовый перевод",
            "conversation_id": conversation_id,
            "confirm_action_id": prepared["confirmation_id"],
        }
        executed = request("POST", "/v1/chat", token, json=confirmation)
        replay = request("POST", "/v1/chat", token, json=confirmation)
        assert (
            executed["data"]["result"]["operation_id"] == replay["data"]["result"]["operation_id"]
        )
        print("OK confirmed transfer and idempotent replay")

    card = httpx.get(f"{TRAVEL_AGENT}/.well-known/agent-card.json", timeout=5).json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    print("OK A2A Agent Card")
    print("Smoke test completed successfully")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-test the local Agentic Banking stand")
    parser.add_argument(
        "--confirm", action="store_true", help="execute a 1 RUB self-owned transfer and replay it"
    )
    parser.add_argument(
        "--expect-llm-unavailable",
        action="store_true",
        help="assert that chat fails explicitly when required LLM settings are absent",
    )
    try:
        args = parser.parse_args()
        run(args.confirm, args.expect_llm_unavailable)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
