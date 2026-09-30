from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app


class DuffelClient:
    """Small HTTP adapter for the current Duffel Flights and Stays APIs."""

    def __init__(self) -> None:
        self.access_token = os.getenv("DUFFEL_ACCESS_TOKEN", "")
        self.base_url = os.getenv("DUFFEL_BASE_URL", "https://api.duffel.com").rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.access_token)

    async def post(self, path: str, payload: dict[str, Any], *, request_timeout: float = 35) -> Any:
        if not self.configured:
            raise ValueError("Duffel live provider is unconfigured: set DUFFEL_ACCESS_TOKEN")
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.post(
                f"{self.base_url}{path}",
                json={"data": payload},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Duffel-Version": "v2",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "gzip",
                },
            )
        if response.is_error:
            try:
                body = response.json()
                raw_errors = body.get("errors")
                errors = (
                    raw_errors.get("errors", [])
                    if isinstance(raw_errors, dict)
                    else raw_errors or []
                )
                detail = errors[0].get("message") if isinstance(errors, list) and errors else body
            except ValueError:
                detail = response.text
            request_id = response.headers.get("x-request-id", "unknown")
            raise ValueError(
                f"Duffel {path} failed ({response.status_code}, request_id={request_id}): {detail}"
            )
        return response.json().get("data", {})


duffel = DuffelClient()


async def provider_status(_: dict[str, Any], __: Principal) -> dict[str, Any]:
    return {
        "provider": "duffel",
        "mode": "live" if duffel.configured else "unconfigured",
        "capabilities": ["flights", "stays"],
    }


async def flights_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    departure = date.fromisoformat(str(arguments["departure_date"]))
    if departure < date.today():
        raise ValueError("departure_date cannot be in the past")
    origin = str(arguments["origin"]).upper()
    destination = str(arguments["destination"]).upper()
    slices = [
        {"origin": origin, "destination": destination, "departure_date": departure.isoformat()}
    ]
    if arguments.get("return_date"):
        return_date = date.fromisoformat(str(arguments["return_date"]))
        if return_date <= departure:
            raise ValueError("return_date must be later than departure_date")
        slices.append(
            {
                "origin": destination,
                "destination": origin,
                "departure_date": return_date.isoformat(),
            }
        )
    adults = int(arguments.get("adults", 1))
    data = await duffel.post(
        "/air/offer_requests?return_offers=true&supplier_timeout=15000",
        {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": str(arguments.get("cabin_class", "economy")),
        },
        request_timeout=25,
    )
    offers: list[dict[str, Any]] = []
    for raw in (data.get("offers") or [])[: int(arguments.get("max_results", 5))]:
        offer_slices = raw.get("slices") or []
        outbound = offer_slices[0] if offer_slices else {}
        segments = outbound.get("segments") or []
        first = segments[0] if segments else {}
        last = segments[-1] if segments else {}
        owner = raw.get("owner") or first.get("operating_carrier") or {}
        offers.append(
            {
                "offer_id": str(raw.get("id", "")),
                "origin": ((first.get("origin") or {}).get("iata_code") or origin),
                "destination": ((last.get("destination") or {}).get("iata_code") or destination),
                "departure_at": first.get("departing_at"),
                "arrival_at": last.get("arriving_at"),
                "carrier": owner.get("name") or owner.get("iata_code"),
                "stops": max(len(segments) - 1, 0),
                "price": float(raw.get("total_amount", 0)),
                "currency": raw.get("total_currency"),
                "expires_at": raw.get("expires_at"),
            }
        )
    return {"provider": "duffel", "mode": "live", "offers": offers}


def _rate_is_refundable(accommodation: dict[str, Any]) -> bool:
    for room in accommodation.get("rooms") or []:
        for rate in room.get("rates") or []:
            if rate.get("cancellation_timeline") or rate.get("refundable") is True:
                return True
    return False


async def accommodations_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    check_in = date.fromisoformat(str(arguments["check_in"]))
    check_out = date.fromisoformat(str(arguments["check_out"]))
    if check_in < date.today() or check_out <= check_in:
        raise ValueError("check-in/check-out dates are invalid")
    adults = int(arguments.get("adults", 1))
    data = await duffel.post(
        "/stays/search",
        {
            "rooms": 1,
            "guests": [{"type": "adult"} for _ in range(adults)],
            "check_in_date": check_in.isoformat(),
            "check_out_date": check_out.isoformat(),
            "location": {
                "radius": int(arguments.get("radius_km", 10)),
                "geographic_coordinates": {
                    "latitude": float(arguments["latitude"]),
                    "longitude": float(arguments["longitude"]),
                },
            },
            "free_cancellation_only": bool(arguments.get("free_cancellation_only", False)),
        },
        request_timeout=40,
    )
    nights = (check_out - check_in).days
    offers: list[dict[str, Any]] = []
    for raw in (data.get("results") or [])[: int(arguments.get("max_results", 10))]:
        accommodation = raw.get("accommodation") or {}
        offers.append(
            {
                "offer_id": str(raw.get("id", "")),
                "hotel_id": accommodation.get("id"),
                "name": accommodation.get("name"),
                "rating": accommodation.get("rating"),
                "nights": nights,
                "price": float(raw.get("cheapest_rate_total_amount", 0)),
                "currency": raw.get("cheapest_rate_currency"),
                "refundable": _rate_is_refundable(accommodation),
                "expires_at": raw.get("expires_at"),
            }
        )
    return {"provider": "duffel", "mode": "live", "offers": offers}


tools = [
    MCPTool(
        "provider_status",
        "Report whether the live Duffel Flights and Stays provider has an access token.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        provider_status,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
    MCPTool(
        "flights_search",
        "Search live Duffel flight offers without creating an order.",
        {
            "type": "object",
            "required": ["origin", "destination", "departure_date"],
            "properties": {
                "origin": {"type": "string", "minLength": 3, "maxLength": 3},
                "destination": {"type": "string", "minLength": 3, "maxLength": 3},
                "departure_date": {"type": "string", "format": "date"},
                "return_date": {"type": "string", "format": "date"},
                "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
                "cabin_class": {
                    "type": "string",
                    "enum": ["economy", "premium_economy", "business", "first"],
                    "default": "economy",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "additionalProperties": False,
        },
        flights_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
    MCPTool(
        "accommodations_search",
        "Search live Duffel Stays accommodation rates without booking.",
        {
            "type": "object",
            "required": ["latitude", "longitude", "check_in", "check_out"],
            "properties": {
                "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                "longitude": {"type": "number", "minimum": -180, "maximum": 180},
                "check_in": {"type": "string", "format": "date"},
                "check_out": {"type": "string", "format": "date"},
                "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
                "radius_km": {"type": "integer", "minimum": 1, "maximum": 50},
                "free_cancellation_only": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
        accommodations_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
]

app = create_mcp_app("travel-data-mcp", tools)
