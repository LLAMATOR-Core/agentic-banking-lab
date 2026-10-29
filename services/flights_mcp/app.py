from __future__ import annotations

import hashlib
import hmac
import os
from datetime import date
from typing import Any

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common.auth import Principal, require_principal
from services.common.mcp import MCPTool, create_mcp_app


class FlightSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    currency: str = Field(default="rub", pattern=r"^[a-zA-Z]{3}$")
    max_results: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def validate_trip(self) -> FlightSearch:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.departure_date < date.today():
            raise ValueError("departure_date cannot be in the past")
        if self.return_date is not None and self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        return self


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


async def search_flights(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    query = FlightSearch.model_validate(arguments)
    canonical = query.model_dump(mode="json", exclude={"max_results"})
    canonical["currency"] = query.currency.upper()
    seed = hashlib.sha256(str(sorted(canonical.items())).encode()).hexdigest()
    base_price = 100 + int(seed[:8], 16) % 400
    results = []
    for index in range(query.max_results):
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
        results.append(
            {
                "result_id": f"flight-mock-{seed[:12]}-{index + 1}",
                "provider": "flights-mcp",
                "mode": "mock-external-mcp",
                "synthetic": True,
                "title": f"[МОК] {query.origin} → {query.destination}, вариант {index + 1}",
                "airline": f"Mock Air {index + 1}",
                "legs": legs,
                "adults": query.adults,
                "price": (base_price + index * 25) * query.adults * len(legs),
                "currency": query.currency.upper(),
                "price_scope": "all-passengers-all-legs",
                "booking_supported": False,
                "content_trust": "untrusted-external",
                "disclaimer": "Синтетический рейс и цена для тестов. Реального билета нет.",
            }
        )
    return {
        "provider": "flights-mcp",
        "mode": "mock-external-mcp",
        "synthetic": True,
        "query": canonical,
        "results": results,
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
            FlightSearch.model_json_schema(),
            search_flights,
            {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        ),
    ],
)
app.dependency_overrides[require_principal] = require_provider_token
