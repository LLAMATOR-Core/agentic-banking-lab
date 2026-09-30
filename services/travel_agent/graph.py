from __future__ import annotations

import asyncio
import os
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from services.common.auth import ServiceTokenProvider
from services.common.mcp import MCPClient

CITY_COORDINATES = {
    "IST": (41.0082, 28.9784),
    "LON": (51.5074, -0.1278),
    "PAR": (48.8566, 2.3522),
    "NYC": (40.7128, -74.0060),
    "DXB": (25.2048, 55.2708),
    "MAD": (40.4168, -3.7038),
}


class TravelState(TypedDict, total=False):
    request: dict[str, Any]
    token: str
    flights: dict[str, Any]
    accommodations: dict[str, Any]
    recommendations: dict[str, Any]
    live_error: str | None
    result: dict[str, Any]
    tool_calls: list[str]


class TravelGraph:
    def __init__(self) -> None:
        self.booking = MCPClient(os.getenv("BOOKING_MCP_URL", "http://booking-mcp:8102/mcp"))
        self.expedia = MCPClient(os.getenv("EXPEDIA_MCP_URL", "http://expedia-mcp:8103/mcp"))
        self.travel_data = MCPClient(
            os.getenv("TRAVEL_DATA_MCP_URL", "http://travel-data-mcp:8106/mcp"), timeout=35
        )
        self.tokens = ServiceTokenProvider()
        builder = StateGraph(TravelState)
        builder.add_node("authorize_tools", self.authorize_tools)
        builder.add_node("search_sources", self.search_sources)
        builder.add_node("get_recommendations", self.get_recommendations)
        builder.add_node("assemble", self.assemble)
        builder.add_edge(START, "authorize_tools")
        builder.add_edge("authorize_tools", "search_sources")
        builder.add_edge("search_sources", "get_recommendations")
        builder.add_edge("get_recommendations", "assemble")
        builder.add_edge("assemble", END)
        self.graph = builder.compile()

    async def authorize_tools(self, _: TravelState) -> TravelState:
        return {"token": await self.tokens.get(), "tool_calls": []}

    async def search_sources(self, state: TravelState) -> TravelState:
        request = state["request"]
        flight_args = {
            "origin": request["origin"],
            "destination": request["destination_airport"],
            "departure_date": request["departure_date"],
            "return_date": request["return_date"],
            "adults": request.get("adults", 1),
        }
        accommodation_args = {
            "destination": request["destination"],
            "check_in": request["departure_date"],
            "check_out": request["return_date"],
            "adults": request.get("adults", 1),
        }
        live_flight_args = {**flight_args, "max_results": 5}
        latitude = request.get("latitude")
        longitude = request.get("longitude")
        if latitude is None or longitude is None:
            latitude, longitude = CITY_COORDINATES.get(
                str(request["destination_airport"]).upper(), (None, None)
            )
        live_hotel_args = {
            "latitude": latitude,
            "longitude": longitude,
            "check_in": request["departure_date"],
            "check_out": request["return_date"],
            "adults": request.get("adults", 1),
            "max_results": 10,
        }
        mock_flights, mock_accommodations = await asyncio.gather(
            self.booking.call("flights_search", flight_args, state["token"]),
            self.booking.call("accommodations_search", accommodation_args, state["token"]),
        )
        live_error: str | None = None
        try:
            if latitude is None or longitude is None:
                raise ValueError(
                    "Live stay search requires latitude/longitude for this destination"
                )
            live_flights, live_accommodations = await asyncio.gather(
                self.travel_data.call("flights_search", live_flight_args, state["token"]),
                self.travel_data.call("accommodations_search", live_hotel_args, state["token"]),
            )
        except (ValueError, httpx.HTTPError) as exc:
            live_error = str(exc)
            live_flights = {"offers": [], "mode": "unavailable"}
            live_accommodations = {"offers": [], "mode": "unavailable"}
        use_live = bool(live_flights.get("offers") and live_accommodations.get("offers"))
        flights = live_flights if use_live else mock_flights
        accommodations = live_accommodations if use_live else mock_accommodations
        return {
            "flights": flights,
            "accommodations": accommodations,
            "live_error": live_error,
            "tool_calls": [
                *state.get("tool_calls", []),
                "duffel.flights_search",
                "duffel.accommodations_search",
                "booking.flights_search",
                "booking.accommodations_search",
            ],
        }

    async def get_recommendations(self, state: TravelState) -> TravelState:
        request = state["request"]
        recommendations = await self.expedia.call(
            "travel_recommendations",
            {
                "destination": request["destination"],
                "interests": request.get("interests", []),
                "budget": request.get("budget"),
                "currency": request.get("currency", "RUB"),
            },
            state["token"],
        )
        return {
            "recommendations": recommendations,
            "tool_calls": [*state.get("tool_calls", []), "expedia.travel_recommendations"],
        }

    async def assemble(self, state: TravelState) -> TravelState:
        flights = state["flights"]["offers"]
        hotels = state["accommodations"]["offers"]
        combinations: list[dict[str, Any]] = []
        for flight in flights[:2]:
            for hotel in hotels[:2]:
                if flight["currency"] != hotel["currency"]:
                    continue
                combinations.append(
                    {
                        "flight_offer_id": flight["offer_id"],
                        "hotel_offer_id": hotel["offer_id"],
                        "total": flight["price"] + hotel["price"],
                        "currency": flight["currency"],
                        "refundable_hotel": hotel["refundable"],
                    }
                )
        combinations.sort(key=lambda item: item["total"])
        budget = state["request"].get("budget")
        for option in combinations:
            comparable = option["currency"] == state["request"].get("currency", "RUB")
            option["within_budget"] = (
                None if not comparable else budget is None or option["total"] <= budget
            )
        return {
            "result": {
                "destination": state["request"]["destination"],
                "flights": flights,
                "accommodations": hotels,
                "recommendations": state["recommendations"]["recommendations"],
                "packages": combinations,
                "provider_modes": {
                    "flight_search": state["flights"].get("mode"),
                    "hotel_search": state["accommodations"].get("mode"),
                    "booking_mock": "mock",
                    "expedia": state["recommendations"].get("mode"),
                },
                "live_provider_error": state.get("live_error"),
                "disclaimer": "Бронирование не выполнялось; предложения нужно перепроверить.",
            }
        }


travel_graph = TravelGraph()
