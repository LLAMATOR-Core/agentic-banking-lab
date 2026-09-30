from __future__ import annotations

from datetime import date
from hashlib import sha256
from typing import Any

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app


def stable_price(*parts: str, minimum: int, spread: int) -> int:
    digest = sha256("|".join(parts).encode()).digest()
    return minimum + int.from_bytes(digest[:2], "big") % spread


async def flights_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    origin = str(arguments["origin"]).upper()
    destination = str(arguments["destination"]).upper()
    departure = date.fromisoformat(str(arguments["departure_date"]))
    adults = int(arguments.get("adults", 1))
    if departure < date.today():
        raise ValueError("departure_date cannot be in the past")
    base = stable_price(origin, destination, str(departure), minimum=9500, spread=22000)
    return {
        "provider": "booking-mcp-lab-adapter",
        "mode": "mock",
        "offers": [
            {
                "offer_id": f"FL-{origin}-{destination}-01",
                "origin": origin,
                "destination": destination,
                "departure_at": f"{departure.isoformat()}T07:25:00+03:00",
                "arrival_at": f"{departure.isoformat()}T10:40:00+03:00",
                "carrier": "Demo Air",
                "stops": 0,
                "price": base * adults,
                "currency": "RUB",
            },
            {
                "offer_id": f"FL-{origin}-{destination}-02",
                "origin": origin,
                "destination": destination,
                "departure_at": f"{departure.isoformat()}T13:10:00+03:00",
                "arrival_at": f"{departure.isoformat()}T18:15:00+03:00",
                "carrier": "InterOp Airlines",
                "stops": 1,
                "price": int(base * 0.82) * adults,
                "currency": "RUB",
            },
        ],
    }


async def accommodations_search(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    destination = str(arguments["destination"])
    check_in = date.fromisoformat(str(arguments["check_in"]))
    check_out = date.fromisoformat(str(arguments["check_out"]))
    if check_in < date.today() or check_out <= check_in:
        raise ValueError("check-in/check-out dates are invalid")
    nights = (check_out - check_in).days
    base = stable_price(destination, str(check_in), minimum=4200, spread=7200)
    return {
        "provider": "booking-mcp-lab-adapter",
        "mode": "mock",
        "offers": [
            {
                "offer_id": "HTL-CENTRAL-01",
                "name": f"{destination} Central Hotel",
                "rating": 8.7,
                "nights": nights,
                "price": base * nights,
                "currency": "RUB",
                "refundable": True,
            },
            {
                "offer_id": "HTL-BUDGET-02",
                "name": f"{destination} Riverside Rooms",
                "rating": 8.1,
                "nights": nights,
                "price": int(base * 0.72) * nights,
                "currency": "RUB",
                "refundable": False,
            },
        ],
    }


tools = [
    MCPTool(
        "flights_search",
        "Search flight offers. Lab adapter returns deterministic synthetic offers.",
        {
            "type": "object",
            "required": ["origin", "destination", "departure_date"],
            "properties": {
                "origin": {"type": "string", "minLength": 3, "maxLength": 3},
                "destination": {"type": "string", "minLength": 3, "maxLength": 3},
                "departure_date": {"type": "string", "format": "date"},
                "return_date": {"type": "string", "format": "date"},
                "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
            },
            "additionalProperties": False,
        },
        flights_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
    MCPTool(
        "accommodations_search",
        "Search accommodation offers. Lab adapter returns deterministic synthetic offers.",
        {
            "type": "object",
            "required": ["destination", "check_in", "check_out"],
            "properties": {
                "destination": {"type": "string"},
                "check_in": {"type": "string", "format": "date"},
                "check_out": {"type": "string", "format": "date"},
                "adults": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
            },
            "additionalProperties": False,
        },
        accommodations_search,
        {"readOnlyHint": True, "openWorldHint": True},
    ),
]

app = create_mcp_app("booking-mcp", tools)
