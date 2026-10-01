from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.banking_agent.skills import BankingSkills
from services.common.mcp import MCPClient


def test_compose_uses_real_booking_and_aviasales_mcp_servers() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    graph = Path("services/travel_agent/graph.py").read_text(encoding="utf-8")

    assert "github.com/stufently/aviasales-mcp.git" in compose
    assert "https://mcp.hasdata.com/mcp?apis=booking" in compose
    assert "hasdata_booking_search_getBookingSearchResults" in graph
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
            search_accommodations=True,
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
    assert result["plan"]["search_accommodations"] is False
    assert result["plan"]["get_recommendations"] is True

