"""Search outcomes survive MCP and A2A without invented source data."""

import importlib
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
from fastapi.testclient import TestClient

from services.duckduckgo_mcp import app as search


@pytest.mark.parametrize("tool", ["text_search", "news_search"])
@pytest.mark.parametrize(
    "outcome,status,code",
    [
        ([{"title": "Источник", "href": "https://example.org"}], "ok", None),
        ([], "empty", None),
        (DDGSException("No results found."), "empty", "no_results"),
        (TimeoutException("timed out"), "unavailable", "timeout"),
        (TimeoutError(), "unavailable", "timeout"),
        (RatelimitException("429"), "unavailable", "rate_limited"),
        (DDGSException("upstream failed"), "unavailable", "provider_error"),
    ],
)
def test_search_outcomes_are_structured_and_traced(monkeypatch, tool, outcome, status, code):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    updates = []

    @contextmanager
    def observe(*args, **kwargs):
        yield SimpleNamespace(update=lambda **values: updates.append(values))

    def provider(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        search, "DDGS", lambda **kwargs: SimpleNamespace(text=provider, news=provider)
    )
    monkeypatch.setattr(search, "observation", observe)
    with TestClient(search.app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"query": "Стамбул"}},
            },
        )
    assert response.status_code == 200
    wire = response.json()["result"]
    assert wire["isError"] is False
    result = wire["structuredContent"]
    assert result["status"] == status
    assert result["mode"] == ("unavailable" if status == "unavailable" else "live")
    assert bool(result["results"]) == (status == "ok")
    assert (result["diagnostic"]["code"] if result["diagnostic"] else None) == code
    assert updates[-1]["output"] == result
    assert updates[-1]["level"] == ("WARNING" if status == "unavailable" else "DEFAULT")


@pytest.fixture
def travel(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    module = importlib.import_module("services.travel_agent.app")
    graph = module.travel_graph

    async def generate(schema, **kwargs):
        assert kwargs["operation"] == "plan_tools"  # Empty sources must not be invented by an LLM.
        return schema(search_flights=False, get_recommendations=True, rationale="Сведения о месте")

    monkeypatch.setattr(graph.tokens, "get", AsyncMock(return_value="test-token"))
    monkeypatch.setattr(graph.llm, "generate_json", AsyncMock(side_effect=generate))
    return module, graph


@pytest.mark.parametrize(
    "outcome,status,has_error",
    [
        ({"results": [], "mode": "live", "status": "empty"}, "empty", False),
        (
            {
                "results": [],
                "mode": "unavailable",
                "status": "unavailable",
                "diagnostic": {"code": "timeout", "detail": "timed out"},
            },
            "unavailable",
            True,
        ),
        (httpx.ConnectError("MCP unreachable"), "unavailable", True),
        (ValueError("Old MCP error"), "unavailable", True),
    ],
)
def test_a2a_returns_explanation_instead_of_500(travel, monkeypatch, outcome, status, has_error):
    module, graph = travel
    call = AsyncMock(
        **(
            {"side_effect": outcome}
            if isinstance(outcome, Exception)
            else {"return_value": outcome}
        )
    )
    monkeypatch.setattr(graph.destination_search, "call", call)
    with TestClient(module.app) as client:
        response = client.post(
            "/a2a/v1/message:send",
            json={
                "message": {
                    "role": "ROLE_USER",
                    "contextId": "failed-search-conversation",
                    "parts": [
                        {
                            "data": {
                                "user_id": "customer-001",
                                "request_type": "destination_info",
                                "destination": "Стамбул",
                            }
                        }
                    ],
                }
            },
        )
    assert response.status_code == 200, response.text
    result = response.json()["message"]["parts"][0]["data"]
    assert result["destination_search_meta"]["status"] == status
    assert result["recommendations"] == [] and result["flights"] == []
    assert bool(result["search_errors"]) == has_error
    assert result["agent_cautions"]
    assert ("Поиск не вернул" if status == "empty" else "не удалось получить") in result[
        "agent_summary"
    ]
    assert "llm.rank_search_results" not in result["tool_calls"]
    assert "duckduckgo.text_search" in result["tool_calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_search_authorization_errors_are_not_hidden(travel, monkeypatch, status):
    _, graph = travel
    response = httpx.Response(status, request=httpx.Request("POST", "http://search/mcp"))
    monkeypatch.setattr(
        graph.destination_search,
        "call",
        AsyncMock(
            side_effect=httpx.HTTPStatusError("denied", request=response.request, response=response)
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await graph.get_recommendations(
            {
                "request": {"destination": "Стамбул"},
                "plan": {"get_recommendations": True},
                "token": "test-token",
            }
        )


@pytest.mark.asyncio
async def test_failed_destination_search_preserves_flights_and_other_errors(travel, monkeypatch):
    _, graph = travel
    # A transport timeout from httpx is handled; arbitrary programming errors are not.
    monkeypatch.setattr(
        graph.destination_search, "call", AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    )
    state = {
        "request": {"destination": "Стамбул"},
        "plan": {"get_recommendations": True},
        "token": "test-token",
        "search_errors": ["flights: earlier diagnostic"],
        "flights": {"results": [{"result_id": "flight-1"}], "mode": "live-mcp-read-only"},
    }
    state.update(await graph.get_recommendations(state))
    assert state["search_errors"][0] == "flights: earlier diagnostic"

    async def generate(schema, **kwargs):
        assert kwargs["payload"]["destination_search_meta"]["status"] == "unavailable"
        assert len(kwargs["payload"]["search_errors"]) == 2
        return schema(summary="Найден рейс.", recommended_flight_ids=["flight-1"])

    monkeypatch.setattr(graph.llm, "generate_json", generate)
    result = (await graph.assemble(state))["result"]
    assert result["flights"] == state["flights"]["results"]
    assert result["agent_cautions"]
    assert result["recommendations"] == []


@pytest.mark.asyncio
async def test_budget_web_search_retries_empty_response_with_a_focused_query(travel, monkeypatch):
    _, graph = travel
    call = AsyncMock(
        side_effect=[
            {"results": [], "mode": "live", "status": "empty"},
            {
                "results": [{"title": "Расходы на Пхукете", "url": "https://example.org/budget"}],
                "mode": "live",
                "status": "ok",
            },
        ]
    )
    monkeypatch.setattr(graph.destination_search, "call", call)
    result = await graph.get_recommendations(
        {
            "request": {
                "request_type": "budget_plan",
                "destination": "Пхукет",
                "departure_date": "2026-12-03",
                "interests": ["PRIVATE_MEMORY_CANARY"],
            },
            "plan": {"get_recommendations": True},
            "token": "test-token",
        }
    )
    queries = [item.args[1]["query"] for item in call.call_args_list]
    assert len(queries) == 2 and queries[0] != queries[1]
    assert "цены" in queries[0] and "Пхукет" in queries[0]
    assert all("PRIVATE_MEMORY_CANARY" not in q for q in queries)
    assert result["recommendations"]["status"] == "ok" and result["search_errors"] == []
    assert [item["status"] for item in result["recommendations"]["attempts"]] == ["empty", "ok"]


@pytest.mark.asyncio
async def test_web_search_does_not_retry_a_rate_limit(travel, monkeypatch):
    _, graph = travel
    call = AsyncMock(
        return_value={
            "results": [],
            "mode": "unavailable",
            "status": "unavailable",
            "diagnostic": {"code": "rate_limited", "detail": "429"},
        }
    )
    monkeypatch.setattr(graph.destination_search, "call", call)
    await graph.get_recommendations(
        {
            "request": {"destination": "Пхукет"},
            "plan": {"get_recommendations": True},
            "token": "test-token",
        }
    )
    call.assert_awaited_once()
