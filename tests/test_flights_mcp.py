from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from services.common.mcp import MCPClient
from services.flights_mcp.app import app


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("FLIGHTS_MCP_AUTH_TOKEN", "provider-secret")
    with TestClient(app) as client:
        yield client


def rpc(client, method, params=None, token="provider-secret"):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        json={"jsonrpc": "2.0", "id": "test", "method": method, "params": params or {}},
    )


def search_args():
    departure = date.today() + timedelta(days=30)
    return {
        "origin": "MOW",
        "destination": "IST",
        "departure_date": departure.isoformat(),
        "return_date": (departure + timedelta(days=5)).isoformat(),
        "adults": 2,
        "currency": "rub",
        "max_results": 3,
    }


@pytest.mark.parametrize(
    "method",
    [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ],
)
@pytest.mark.parametrize("token", [None, "wrong", "debug-token"])
def test_external_auth_is_required_even_when_bank_auth_is_disabled(provider, method, token):
    assert rpc(provider, method, token=token).status_code == 401


def test_empty_configured_secret_fails_closed(provider, monkeypatch):
    monkeypatch.setenv("FLIGHTS_MCP_AUTH_TOKEN", "")
    assert rpc(provider, "tools/list").status_code == 503


def test_handshake_discovery_and_deterministic_synthetic_offers(provider):
    initialized = rpc(provider, "initialize").json()["result"]
    assert initialized["serverInfo"]["name"] == "flights-mcp"
    assert rpc(provider, "notifications/initialized").status_code == 202
    tools = rpc(provider, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search_flights"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert "customer_id" not in tools[0]["inputSchema"]["properties"]
    params = {"name": "search_flights", "arguments": search_args()}
    first = rpc(provider, "tools/call", params).json()["result"]["structuredContent"]
    assert first == rpc(provider, "tools/call", params).json()["result"]["structuredContent"]
    assert first["mode"] == "mock-external-mcp" and first["external_side_effect"] is False
    assert len(first["results"]) == 3
    assert len({item["result_id"] for item in first["results"]}) == 3
    for item in first["results"]:
        assert item["synthetic"] and item["booking_supported"] is False
        assert item["currency"] == "RUB" and item["adults"] == 2
        assert item["legs"][0]["origin"] == "MOW"
        assert item["legs"][1]["destination"] == "MOW"
        assert item["content_trust"] == "untrusted-external"


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "moscow"},
        {"destination": "MOW"},
        {"adults": 0},
        {"adults": 10},
        {"max_results": 0},
        {"max_results": 11},
        {"currency": "not-currency"},
        {"departure_date": "bad"},
        {"departure_date": "2000-01-01"},
        {"return_date": "2000-01-01"},
        {"customer_id": "customer-001"},
    ],
)
def test_invalid_search_returns_tool_error(provider, changes):
    result = rpc(
        provider,
        "tools/call",
        {
            "name": "search_flights",
            "arguments": {**search_args(), **changes},
        },
    ).json()["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result


@pytest.mark.asyncio
async def test_travel_graph_calls_mock_over_mcp_and_preserves_provenance(monkeypatch):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("FLIGHTS_MCP_AUTH_TOKEN", "provider-secret")
    from services.travel_agent.graph import TravelGraph

    graph = object.__new__(TravelGraph)
    graph.flights = MCPClient("http://provider/mcp")

    async def post(payload, bearer_token):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://provider",
        ) as client:
            return await client.post(
                "/mcp",
                json=payload,
                headers={"Authorization": "Bearer provider-secret"},
            )

    monkeypatch.setattr(graph.flights, "_post", post)
    arguments = search_args()
    request = {
        **arguments,
        "destination_airport": arguments["destination"],
        "user_id": "must-not-reach-provider",
    }
    result = await graph._search_flights(request)
    assert result["mode"] == "mock-external-mcp"
    assert len(result["results"]) == 10
    assert all(item["synthetic"] for item in result["results"])
    assert all(item["result_id"].startswith("flight-mock-") for item in result["results"])
    assert "must-not-reach-provider" not in str(result)


def test_one_way_search(provider):
    arguments = search_args()
    arguments["return_date"] = None
    result = rpc(
        provider,
        "tools/call",
        {
            "name": "search_flights",
            "arguments": arguments,
        },
    ).json()["result"]["structuredContent"]
    assert len(result["results"][0]["legs"]) == 1


@pytest.mark.asyncio
async def test_provider_failure_is_reported_without_local_fallback(monkeypatch):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent-test")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    from services.travel_agent.graph import TravelGraph

    graph = object.__new__(TravelGraph)

    async def unavailable(request):
        raise ValueError("Flights MCP is unavailable")

    monkeypatch.setattr(graph, "_search_flights", unavailable)
    result = await graph.search_sources(
        {
            "request": search_args(),
            "plan": {"search_flights": True, "search_accommodations": False},
        }
    )
    assert result["flights"] == {"results": [], "mode": "unavailable"}
    assert result["search_errors"] == ["flights: Flights MCP is unavailable"]
