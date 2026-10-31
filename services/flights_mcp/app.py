from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException

from services.common.auth import Principal, require_principal
from services.common.mcp import MCPTool, create_mcp_app
from services.common.schemas import MockFlightSearch


async def require_provider_token(authorization: str | None = Header(default=None)) -> Principal:
    # Deliberately outside the bank's Keycloak domain. AUTH_DISABLED does not bypass this.
    secret = os.getenv("FLIGHTS_MCP_AUTH_TOKEN", "local-flights-mcp")
    if not secret:
        raise HTTPException(status_code=503, detail="Flights MCP bearer secret is unconfigured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), secret.encode()):
        raise HTTPException(
            status_code=401,
            detail="Valid Flights MCP bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal("travel-provider-client", "travel-agent", None, "external-client", {})


def read_provider_note(path: str) -> str:
    with Path(path).open(encoding="utf-8") as note:
        return note.read(4000)


async def search_flights(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    query = MockFlightSearch.model_validate(arguments)
    canonical = query.model_dump(mode="json", exclude={"max_results"})
    canonical["currency"] = query.currency.upper()
    note_file = os.getenv("FLIGHTS_MCP_NOTE_FILE")
    provider_note = await asyncio.to_thread(read_provider_note, note_file) if note_file else ""
    legs = [
        {
            "origin": query.origin,
            "destination": query.destination,
            "date": query.departure_date.isoformat(),
        }
    ]
    if query.return_date:
        legs.append(
            {
                "origin": query.destination,
                "destination": query.origin,
                "date": query.return_date.isoformat(),
            }
        )
    return {
        "provider": "flights-mcp",
        "mode": "mock-external-mcp",
        "synthetic": True,
        "query": canonical,
        "results": [
            {
                "result_id": "flight-mock-1",
                "provider": "flights-mcp",
                "mode": "mock-external-mcp",
                "synthetic": True,
                "title": f"[МОК] {query.origin} → {query.destination}",
                "airline": "Mock Air",
                "legs": legs,
                "adults": query.adults,
                "price": 1000 * query.adults * len(legs),
                "currency": query.currency.upper(),
                "price_scope": "all-passengers-all-legs",
                "booking_supported": False,
                "content_trust": "untrusted-external",
                "provider_note": provider_note,
                "disclaimer": "Синтетический рейс и цена для тестов. Реального билета нет.",
            }
        ],
        "content_trust": "untrusted-external",
        "booking_supported": False,
        "external_side_effect": False,
    }


app = create_mcp_app(
    "flights-mcp",
    [
        MCPTool(
            "search_flights",
            "Search synthetic flight offers from a mock external provider. "
            "Prices and flights are fictional; no booking or airline API calls are made.",
            MockFlightSearch.model_json_schema(),
            search_flights,
            {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        ),
    ],
)
app.dependency_overrides[require_principal] = require_provider_token
