from __future__ import annotations

from typing import Any

from services.common.auth import Principal
from services.common.mcp import MCPTool, create_mcp_app


async def travel_recommendations(arguments: dict[str, Any], _: Principal) -> dict[str, Any]:
    destination = str(arguments["destination"])
    interests = arguments.get("interests") or ["culture", "food"]
    return {
        "provider": "expedia-mcp-lab-adapter",
        "mode": "mock",
        "destination": destination,
        "recommendations": [
            {
                "title": f"Исторический центр: {destination}",
                "category": "culture",
                "why": "Пешеходный маршрут без дополнительных транспортных расходов.",
            },
            {
                "title": f"Локальная кухня: {destination}",
                "category": "food",
                "why": "Подборка рынков и ресторанов со средним бюджетом.",
            },
            {
                "title": "День без плотной программы",
                "category": "wellbeing",
                "why": "Оставляет запас времени при задержке рейса.",
            },
        ],
        "matched_interests": interests,
        "disclaimer": "Synthetic recommendations for interoperability testing; not bookable.",
    }


tools = [
    MCPTool(
        "travel_recommendations",
        "Return destination ideas based on interests and budget. Synthetic lab data.",
        {
            "type": "object",
            "required": ["destination"],
            "properties": {
                "destination": {"type": "string"},
                "interests": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "budget": {"type": "number", "minimum": 0},
                "currency": {"type": "string", "default": "RUB"},
            },
            "additionalProperties": False,
        },
        travel_recommendations,
        {"readOnlyHint": True, "openWorldHint": True},
    )
]

app = create_mcp_app("expedia-mcp", tools)
