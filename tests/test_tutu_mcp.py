import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("FLIGHTS_MCP_AUTH_TOKEN", "mock-secret-must-stay-local")
    monkeypatch.delenv("FLIGHTS_PROVIDER", raising=False)
    monkeypatch.delenv("TUTU_MCP_URL", raising=False)
    module = importlib.import_module("services.travel_agent.graph")
    return module.TravelGraph()


def trip():
    return {
        "origin": "MOW",
        "destination": "Санкт-Петербург",
        "destination_airport": "LED",
        "departure_date": "2026-11-30",
        "return_date": "2026-12-05",
        "adults": 2,
        "currency": "USD",
        "user_id": "customer-must-not-reach-tutu",
    }


@pytest.fixture
def live_snapshot():
    # Shape captured from the public read-only endpoint on 2026-10-31; opaque ids shortened.
    return json.loads(Path("tests/fixtures/tutu_avia.json").read_text())


@pytest.mark.asyncio
async def test_tutu_is_default_and_preserves_party_price_itinerary_and_pagination(
    graph,
    monkeypatch,
    live_snapshot,
):
    calls = []

    async def list_tools():
        return [
            {
                "name": "search_avia",
                "inputSchema": {
                    "properties": {
                        key: {}
                        for key in (
                            "origin",
                            "destination",
                            "departure_date",
                            "return_date",
                            "adults",
                            "page_size",
                            "view",
                        )
                    }
                },
            }
        ]

    async def call(name, arguments, bearer_token=None):
        calls.append((name, arguments, bearer_token))
        return copy.deepcopy(live_snapshot)

    assert graph.flight_provider == "tutu"
    assert graph.flights.endpoint == "https://mcp.tutu.ru/mcp"
    assert graph.flights.headers == {}
    monkeypatch.setattr(graph.flights, "list_tools", list_tools)
    monkeypatch.setattr(graph.flights, "call", call)
    result = await graph._search_flights(trip())
    assert calls == [
        (
            "search_avia",
            {
                "origin": "MOW",
                "destination": "LED",
                "departure_date": "2026-11-30",
                "return_date": "2026-12-05",
                "adults": 2,
                "page_size": 10,
                "view": "compact",
            },
            None,
        )
    ]
    offer = result["results"][0]
    original = live_snapshot["offers"][0]
    assert result["provider"] == "tutu-mcp"
    assert result["mode"] == "live-mcp-read-only"
    assert offer["synthetic"] is False
    assert offer["price"] == original["price"]  # Already whole party, no USD relabel/conversion.
    assert offer["legs"] == original["legs"]
    assert offer["variants"][0]["conditions"] == original["variants"][0]["conditions"]
    assert offer["search_results_url"] == original["search_results_url"]
    assert "checkout_ref" not in offer and "offer_hash" not in offer["variants"][0]
    assert "checkout_hint" not in result["meta"]
    assert result["meta"]["has_more"] is True
    assert result["meta"]["total_matched_exact"] is False
    assert result["meta"]["pricing"]["basis"] == "party_total"
    assert result["booking_supported"] is False


@pytest.mark.asyncio
async def test_schema_drift_is_an_error_even_when_tutu_declares_no_required_fields(
    graph, monkeypatch
):
    async def list_tools():
        return [{"name": "search_avia", "inputSchema": {"properties": {}}}]

    monkeypatch.setattr(graph.flights, "list_tools", list_tools)
    with pytest.raises(ValueError, match="schema changed"):
        await graph._search_flights(trip())


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"offers": []}, {"text": "unexpected"}])
async def test_empty_results_and_protocol_errors_are_not_replaced_with_mock(
    graph, monkeypatch, response
):
    async def call_adapted(*args, **kwargs):
        return response

    monkeypatch.setattr(graph, "_call_adapted_tool", call_adapted)
    result = await graph.search_sources(
        {
            "request": trip(),
            "plan": {"search_flights": True},
        }
    )
    assert result["flights"]["results"] == []
    assert result["tool_calls"] == ["tutu.search_avia"]
    assert result["flights"]["mode"] == (
        "live-mcp-read-only" if "offers" in response else "unavailable"
    )
    assert bool(result["search_errors"]) == ("offers" not in response)


def test_mock_requires_explicit_provider_selection(graph, monkeypatch):
    monkeypatch.setenv("FLIGHTS_PROVIDER", "mock")
    mock = type(graph)()
    assert mock.flights.endpoint == "http://flights-mcp:8106/mcp"
    assert mock.flights.headers == {"Authorization": "Bearer mock-secret-must-stay-local"}
    monkeypatch.setenv("FLIGHTS_PROVIDER", "typo")
    with pytest.raises(ValueError, match="FLIGHTS_PROVIDER"):
        type(graph)()
