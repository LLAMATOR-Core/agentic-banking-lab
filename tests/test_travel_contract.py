from __future__ import annotations

import importlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from services.banking_agent.skills import BankingSkills
from services.common.mcp import MCPClient


def test_compose_uses_tutu_by_default_and_keeps_separate_mock() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    graph = Path("services/travel_agent/graph.py").read_text(encoding="utf-8")

    assert "services.flights_mcp.app" in compose
    assert "FLIGHTS_PROVIDER: ${FLIGHTS_PROVIDER:-tutu}" in compose
    assert "https://mcp.tutu.ru/mcp" in compose
    assert "http://flights-mcp:8106/mcp" in compose
    assert "aviasales" not in compose.lower()
    assert "BOOKING_MCP_URL" not in compose
    assert "HASDATA_API_KEY" not in compose
    assert "hasdata_booking_search_getBookingSearchResults" not in graph
    assert '"search_flights"' in graph
    assert '"text_search"' in graph
    assert "expedia-mcp" not in compose


def test_mcp_client_decodes_streamable_http_event() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":"1","result":{"tools":[]}}\n\n',
    )

    assert MCPClient._decode_response(response)["result"] == {"tools": []}


def test_mock_booking_lives_in_travel_agent_and_has_no_external_side_effect(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    module = importlib.import_module("services.travel_agent.app")

    with TestClient(module.app) as client:
        response = client.post(
            "/v1/bookings/mock",
            json={"flight_result_id": "flight-1", "accommodation_result_id": "hotel-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mock-local"
    assert body["external_side_effect"] is False
    assert body["booking_id"].startswith("mock-")


class ExtractionLLM:
    async def generate_json(self, schema, **_: Any):
        return schema(
            request_type="destination_info",
            origin="TBS",
            destination="Ереван",
            destination_airport="EVN",
            departure_date=None,
            return_date=None,
            adults=1,
            budget=None,
            currency="RUB",
            interests=["culture", "history", "food"],
        )


@pytest.mark.asyncio
async def test_natural_language_travel_request_keeps_named_cities() -> None:
    skills = object.__new__(BankingSkills)
    skills.llm = ExtractionLLM()

    request = await skills._trip_request(
        {
            "message": (
                "я живу в Тбилиси и хочу поехать в Ереван, "
                "что интересного есть в Ереване?"
            ),
            "user_id": "customer-001",
        }
    )

    assert request["origin"] == "TBS"
    assert request["destination"] == "Ереван"
    assert request["destination_airport"] == "EVN"
    assert request["request_type"] == "destination_info"


class PlannerLLM:
    async def generate_json(self, schema, **_: Any):
        return schema(
            search_flights=True,
            get_recommendations=False,
            rationale="model proposed too many tools",
        )


@pytest.mark.asyncio
async def test_destination_info_plan_does_not_search_inventory(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    module = importlib.import_module("services.travel_agent.graph")
    graph = object.__new__(module.TravelGraph)
    graph.llm = PlannerLLM()

    result = await graph.plan(
        {
            "request": {
                "request_type": "destination_info",
                "destination": "Ереван",
                "origin": "TBS",
                "destination_airport": "EVN",
            }
        }
    )

    assert result["plan"]["search_flights"] is False
    assert "search_accommodations" not in result["plan"]
    assert result["plan"]["get_recommendations"] is True


@pytest.mark.asyncio
async def test_travel_graph_searches_only_flights_and_destinations_with_legacy_env(monkeypatch):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("FLIGHTS_PROVIDER", "mock")
    # An existing .env must not silently restore the removed integration.
    monkeypatch.setenv("BOOKING_MCP_URL", "https://removed-provider.invalid/mcp")
    monkeypatch.setenv("HASDATA_API_KEY", "REMOVED_PROVIDER_KEY")
    module = importlib.import_module("services.travel_agent.graph")
    graph = module.TravelGraph()
    assert not hasattr(graph, "booking")
    requests = []

    async def generate(schema, *, operation, instructions, payload):
        requests.append(payload)
        if operation == "plan_tools":
            assert "search_accommodations" not in schema.model_fields
            return schema(search_flights=True, get_recommendations=True, rationale="Поиск поездки")
        assert operation == "rank_search_results"
        assert "accommodation_results" not in payload
        assert "recommended_accommodation_ids" not in schema.model_fields
        return schema(recommended_flight_ids=["flight-1"], summary="Найден учебный рейс.")

    monkeypatch.setattr(graph.llm, "generate_json", AsyncMock(side_effect=generate))
    monkeypatch.setattr(graph.tokens, "get", AsyncMock(return_value="travel-token"))
    monkeypatch.setattr(graph.flights, "list_tools", AsyncMock(return_value=[{
        "name": "search_flights",
        "inputSchema": {"properties": {key: {} for key in (
            "origin", "destination", "departure_date", "return_date", "adults",
            "currency", "max_results",
        )}},
    }]))
    monkeypatch.setattr(graph.flights, "call", AsyncMock(return_value={
        "results": [{"synthetic": True, "mode": "mock-external-mcp", "price": 10000}],
    }))
    monkeypatch.setattr(graph.destination_search, "call", AsyncMock(return_value={
        "results": [{"title": "Музеи Еревана"}], "mode": "live-mcp-read-only",
    }))
    departure = date.today() + timedelta(days=30)

    state = await graph.graph.ainvoke({"request": {
        "request_type": "trip_search", "origin": "MOW", "destination": "Ереван",
        "destination_airport": "EVN", "departure_date": departure.isoformat(),
        "return_date": (departure + timedelta(days=7)).isoformat(),
    }})

    result = state["result"]
    assert len(result["flights"]) == 1
    assert result["recommendations"] == [{"title": "Музеи Еревана"}]
    assert result["accommodations"] == []
    assert result["provider_modes"]["hotel_search"] == "not-supported"
    assert result["search_errors"] == []
    assert result["booking"]["external_side_effect"] is False
    assert state["tool_calls"] == [
        "llm.plan_tools", "flights.search_flights", "duckduckgo.text_search",
        "llm.rank_search_results",
    ]
    assert "REMOVED_PROVIDER_KEY" not in json.dumps(requests)
    graph.flights.call.assert_awaited_once()
    graph.destination_search.call.assert_awaited_once()

