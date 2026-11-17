from __future__ import annotations

import os
import uuid
from typing import Any

import httpx

from services.common.auth import ServiceTokenProvider
from services.common.llm import raise_upstream_llm_error
from services.common.tracing import observation, trace_headers


class TravelA2AClient:
    def __init__(self, tokens: ServiceTokenProvider) -> None:
        self.base_url = os.getenv("TRAVEL_AGENT_URL", "http://travel-api:8003")
        self.tokens = tokens
        try:
            self.timeout = float(os.getenv("TRAVEL_HTTP_TIMEOUT_SECONDS", "180"))
        except ValueError:
            self.timeout = 180.0

    async def plan(self, request: dict[str, Any], conversation_id: str) -> dict[str, Any]:
        return await self._send(request, conversation_id)

    async def mock_flight_source(
        self, search: dict[str, Any], conversation_id: str
    ) -> dict[str, Any]:
        return await self._send(
            {"request_type": "lab_flight_source", "search": search}, conversation_id
        )

    async def _send(self, request: dict[str, Any], conversation_id: str) -> dict[str, Any]:
        token = await self.tokens.get()
        payload = {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"data": request, "mediaType": "application/json"}],
                "messageId": str(uuid.uuid4()),
                "contextId": conversation_id,
            }
        }
        with observation(
            "a2a.travel.call", input=payload, metadata={"endpoint": self.base_url}
        ) as span:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/a2a/v1/message:send",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        **trace_headers(),
                        "Content-Type": "application/a2a+json",
                        "Accept": "application/a2a+json",
                        "A2A-Version": "1.0",
                    },
                )
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                span.update(output=body, metadata={"http_status": response.status_code})
                raise_upstream_llm_error(response)
                response.raise_for_status()
        return body["message"]["parts"][0]["data"]
