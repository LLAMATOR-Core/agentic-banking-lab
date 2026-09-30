from __future__ import annotations

import os
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common.auth import Principal, require_principal
from services.common.tracing import observation
from services.travel_agent.graph import travel_graph


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=128)
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=2, max_length=120)
    destination_airport: str = Field(min_length=3, max_length=3)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    departure_date: date
    return_date: date
    adults: int = Field(default=1, ge=1, le=9)
    budget: float | None = Field(default=None, gt=0)
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    interests: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.departure_date < date.today():
            raise ValueError("departure_date cannot be in the past")
        if self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        return self


app = FastAPI(title="Partner Travel Agent", version="0.1.0")
Caller = Annotated[Principal, Depends(require_principal)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "travel-agent"}


@app.get("/.well-known/agent-card.json")
async def agent_card() -> dict[str, Any]:
    base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8003")
    return {
        "name": "Bank Partner Travel Agent",
        "description": "Combines live travel search with mock booking and recommendations.",
        "supportedInterfaces": [
            {
                "url": f"{base_url}/a2a/v1",
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "provider": {"organization": "Bank partner", "url": base_url},
        "version": "0.1.0",
        "documentationUrl": f"{base_url}/docs",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            "keycloakOAuth": {
                "type": "oauth2",
                "description": "Keycloak client credentials",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "http://localhost:8080/realms/agentic-banking/protocol/openid-connect/token",
                        "scopes": {},
                    }
                },
            }
        },
        "security": [{"keycloakOAuth": []}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "plan-trip",
                "name": "Plan a trip",
                "description": "Combines flight, accommodation, and destination recommendations.",
                "tags": ["travel", "flights", "accommodation"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
        ],
    }


@app.post("/a2a/v1/message:send")
async def send_message(
    payload: dict[str, Any],
    caller: Caller,
    a2a_version: str = Header(default="1.0", alias="A2A-Version"),
) -> JSONResponse:
    if not a2a_version.startswith("1."):
        raise HTTPException(400, "Only A2A 1.x is supported")
    try:
        message = payload["message"]
        if message.get("role") not in {"ROLE_USER", "user"}:
            raise ValueError("message role must be ROLE_USER")
        data_parts = [
            part["data"] for part in message["parts"] if isinstance(part.get("data"), dict)
        ]
        trip = TripRequest.model_validate(data_parts[0])
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(422, f"Invalid A2A SendMessage request: {exc}") from exc

    safe_input = trip.model_dump(mode="json")
    # The strict schema does not accept bank profile or account data.
    with observation(
        "a2a.travel.send-message",
        input=safe_input,
        metadata={"protocol": "A2A", "version": a2a_version, "caller": caller.authorized_party},
        user_id=trip.user_id,
        session_id=message.get("contextId"),
    ) as span:
        state = await travel_graph.graph.ainvoke({"request": safe_input})
        result = state["result"]
        result["tool_calls"] = state["tool_calls"]
        span.update(output={"packages": len(result["packages"]), "tools": state["tool_calls"]})

    response = {
        "message": {
            "role": "ROLE_AGENT",
            "parts": [{"data": result, "mediaType": "application/json"}],
            "messageId": str(uuid.uuid4()),
            "contextId": message.get("contextId"),
        }
    }
    return JSONResponse(response, media_type="application/a2a+json")
