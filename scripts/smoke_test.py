from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

KEYCLOAK = os.getenv("SMOKE_KEYCLOAK_URL", "http://localhost:8080/realms/agentic-banking").rstrip(
    "/"
)
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
            "client_secret": os.getenv("KEYCLOAK_MEMORY_OPERATOR_SECRET", "memory-operator-secret"),
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


def check_travel_budget(token: str, conversation_id: str) -> None:
    budget = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": "Я хочу поехать в тайланд, давай спланируем мои траты",
            "conversation_id": f"{conversation_id}-budget",
        },
    )
    assert budget["intent"] == "travel"
    assert budget["data"]["missing_fields"]
    assert "destination_airport" not in budget["data"]["missing_fields"]
    assert "a2a.travel.plan-trip" not in budget["tool_calls"]
    assert not any(
        field in budget["message"]
        for field in ("origin", "destination_airport", "departure_date", "return_date")
    )
    print("OK conversational travel budget clarification:", budget["message"])
    followup = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": "Из Москвы, в январе на десять дней",
            "conversation_id": f"{conversation_id}-budget",
        },
    )
    assert followup["intent"] == "travel"
    assert followup["data"]["missing_fields"] == ["adults"], followup
    assert "a2a.travel.plan-trip" not in followup["tool_calls"]
    assert not any(
        field in followup["message"]
        for field in ("origin", "destination_airport", "departure_date", "return_date", "adults")
    )
    print("OK short reply preserves trip details:", followup["message"])


def check_phuket_budget(token: str, conversation_id: str) -> None:
    """Reproduce the reported multi-turn December 2026 outbound itinerary."""
    session = conversation_id + "-phuket"
    initial = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": "Я хочу поехать в тайланд, давай спланируем мои траты",
            "conversation_id": session,
        },
    )
    assert initial["intent"] == "travel"
    replies = []
    for message in [
        "3 взрослых путешественника на пхукет с 3 декабря из петербурга до 20 декабря "
        "и далее перелёт в шанхай — билеты до шанхая не учитывай, эти планы еще могут поменяться",
        "2026 год + у тебя же есть тревел-агент, он умеет составлять планы путешествий",
    ]:
        answer = request(
            "POST",
            "/v1/chat",
            token,
            json={
                "message": message,
                "conversation_id": session,
            },
        )
        assert answer["intent"] == "travel"
        travel = answer["data"].get("travel")
        assert travel, answer["message"]
        assert travel["request_type"] == "budget_plan"
        assert travel["flight_route"] == {
            "origin": "LED",
            "destination_airport": "HKT",
            "departure_date": "2026-12-03",
            "return_date": None,
            "adults": 3,
        }, travel["flight_route"]
        assert any(
            tool in travel["tool_calls"] for tool in ["tutu.search_avia", "flights.search_flights"]
        )
        replies.append(
            {
                "route": travel["flight_route"],
                "flight_mode": travel["provider_modes"]["flight_search"],
                "flight_count": len(travel["flights"]),
                "web_count": len(travel["recommendations"]),
                "search_meta": travel["destination_search_meta"],
                "search_errors": travel["search_errors"],
                "response": answer["message"],
            }
        )
    print(json.dumps({"conversation_id": session, "turns": replies}, ensure_ascii=False, indent=2))
    assert replies[-1]["flight_count"] or replies[-1]["web_count"], (
        "No real search results received"
    )


def check_orchestration(token: str, conversation_id: str) -> None:
    answer = request(
        "POST",
        "/v1/chat",
        token,
        json={
            "message": (
                "Составь предварительный бюджет поездки в Стамбул на 10 дней "
                "в декабре 2026 года для двоих из Санкт-Петербурга. "
                "Сопоставь расходы на поездку с текущими остатками моих счетов "
                "и банковскими расходами."
            ),
            "conversation_id": conversation_id + "-orchestration",
            "travel": {
                "request_type": "budget_plan",
                "destination": "Стамбул",
                "origin": "Санкт-Петербург",
                "travel_period": "декабрь 2026",
                "duration_days": 10,
                "adults": 2,
            },
        },
    )
    data = answer["data"]
    assert set(data["orchestration"]["delegated_agents"]) == {"travel", "operations"}
    assert data["agent_results"]["operations"]["data"]["accounts"]
    assert data["agent_results"]["travel"]["data"]["travel"]["request_type"] == "budget_plan"
    assert "llm.orchestrator_response" in answer["tool_calls"]
    report = Path("artifacts/docker-review/orchestration.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(answer, ensure_ascii=False, indent=2) + "\n")
    print("OK orchestrator combines travel and banking analysis:", answer["message"])


def run(
    confirm: bool,
    expect_llm_unavailable: bool = False,
    travel_budget: bool = False,
    phuket_budget: bool = False,
    orchestration: bool = False,
) -> None:
    wait_for(f"{KEYCLOAK}/.well-known/openid-configuration")
    wait_for(f"{AGENT}/health")
    wait_for(f"{TRAVEL_AGENT}/health")
    wait_for(f"{ORCHESTRATOR}/health")
    token = login()
    conversation_id = f"smoke-{int(time.time())}"
    if orchestration:
        assert httpx.get(f"{AGENT}/health", timeout=5).json()["agent_mode"] == "orchestrator"
        check_orchestration(token, conversation_id)
        return
    if phuket_budget:
        check_phuket_budget(token, conversation_id)
        return
    if travel_budget:
        check_travel_budget(token, conversation_id)
        return
    second_token = login("anna.sokolova")

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

    mode = httpx.get(f"{AGENT}/health", timeout=5).json().get("agent_mode", "legacy")
    if mode == "orchestrator":
        if confirm:
            raise RuntimeError("--confirm requires BANKING_AGENT_MODE=legacy")
        today = date.today()
        month_start = today.replace(day=1).isoformat()
        for message, intent, data_key in [
            (
                f"Дай общий расклад по моим счетам и остаткам, профилю и продуктам АБС. "
                f"Составь выписку с {month_start} по {today.isoformat()} включительно.",
                "operations",
                "statement",
            ),
            ("Проанализируй мой инвестиционный портфель", "investments", "portfolio"),
            ("Что посмотреть в Стамбуле?", "travel", "travel"),
        ]:
            body = {"message": message, "conversation_id": f"{conversation_id}-{intent}"}
            if intent == "travel":
                body["travel"] = {"request_type": "destination_info", "destination": "Стамбул"}
            answer = request("POST", "/v1/chat", token, json=body)
            assert answer["intent"] == intent and data_key in answer["data"]
            assert answer["data"]["experiment"]["run_id"]
            if intent == "operations":
                assert {
                    "customer",
                    "accounts",
                    "operations",
                    "statement",
                    "portfolio",
                    "products",
                } <= answer["data"].keys()
                statement = answer["data"]["statement"]
                assert statement["date_from"] == month_start
                assert statement["date_to"] == today.isoformat()
                print("OK banking overview and dated statement:", answer["message"])
            print(f"OK delegated {intent} skill with experiment evidence")
            if intent == "travel":
                travel_result = answer["data"]["travel"]
                status = travel_result["destination_search_meta"]["status"]
                assert status in ("ok", "empty", "unavailable")
                if status != "ok":
                    assert travel_result["recommendations"] == []
                    assert travel_result["agent_cautions"]
                print(f"OK destination search status={status}: {answer['message']}")
        check_travel_budget(token, conversation_id)
        print("Orchestrator smoke completed; OAuth negatives: scripts/skill_experiment.py")
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
    history = request("GET", f"/v1/conversations/{conversation_id}/messages", token)["messages"]
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
    provider = httpx.get(f"{TRAVEL_AGENT}/health", timeout=5).json()["flight_provider"]
    if provider == "mock":
        assert travel_result["provider_modes"]["flight_search"] == "mock-external-mcp"
        assert len(travel_result["flights"]) == 1
        assert travel_result["flights"][0]["synthetic"]
    else:
        assert travel_result["provider_modes"]["flight_search"] == "live-mcp-read-only"
        assert all(item["synthetic"] is False for item in travel_result["flights"])
    assert travel_result["booking"]["mode"] == "mock-local"
    assert travel_result["booking"]["external_side_effect"] is False
    assert travel_result["accommodations"] == []
    assert travel_result["provider_modes"]["hotel_search"] == "not-supported"
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
    parser.add_argument(
        "--travel-budget",
        action="store_true",
        help="check conversational budget clarification without external travel search",
    )
    parser.add_argument(
        "--phuket-budget",
        action="store_true",
        help="reproduce the reported December 2026 Phuket budget itinerary",
    )
    parser.add_argument(
        "--orchestration",
        action="store_true",
        help="check that the orchestrator consults travel and operations and composes the answer",
    )
    try:
        args = parser.parse_args()
        run(
            args.confirm,
            args.expect_llm_unavailable,
            args.travel_budget,
            args.phuket_budget,
            args.orchestration,
        )
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
