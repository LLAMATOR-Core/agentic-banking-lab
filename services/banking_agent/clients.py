from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from services.common.auth import ServiceTokenProvider


class MemoryClient:
    def __init__(self, tokens: ServiceTokenProvider) -> None:
        self.base_url = os.getenv("MEMORY_URL", "http://memory-orchestrator:8002")
        self.tokens = tokens

    async def recall(
        self, user_id: str, query: str, conversation_id: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        payload = {
            "agent_id": "banking-agent",
            "user_id": user_id,
            "query": query,
            "conversation_id": conversation_id,
            "limit": limit,
        }
        result = await self._request("POST", "/v1/recall", json=payload)
        return result["memories"]

    async def create(
        self,
        *,
        user_id: str,
        memory_type: str,
        content: str,
        conversation_id: str | None,
        metadata: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/memories",
            json={
                "agent_id": "banking-agent",
                "user_id": user_id,
                "memory_type": memory_type,
                "content": content,
                "conversation_id": conversation_id,
                "metadata": metadata,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )

    async def create_shared(
        self,
        *,
        source_user_id: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Write the intentionally shared lab namespace read by every user."""
        return await self.create(
            user_id="*",
            memory_type=memory_type,
            content=content,
            conversation_id=None,
            metadata={
                **metadata,
                "scope": "shared",
                "source_user_id": source_user_id,
            },
        )

    async def get(self, memory_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/memories/{memory_id}")

    async def patch_metadata(self, memory_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/v1/memories/{memory_id}", json={"metadata": metadata}
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self.tokens.get()
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()


class TravelA2AClient:
    def __init__(self, tokens: ServiceTokenProvider) -> None:
        self.base_url = os.getenv("TRAVEL_AGENT_URL", "http://travel-agent:8003")
        self.tokens = tokens

    async def plan(self, request: dict[str, Any], conversation_id: str) -> dict[str, Any]:
        token = await self.tokens.get()
        payload = {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"data": request, "mediaType": "application/json"}],
                "messageId": str(uuid.uuid4()),
                "contextId": conversation_id,
            }
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/a2a/v1/message:send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/a2a+json",
                    "Accept": "application/a2a+json",
                    "A2A-Version": "1.0",
                },
            )
            response.raise_for_status()
        body = response.json()
        return body["message"]["parts"][0]["data"]


def utcnow() -> datetime:
    return datetime.now(UTC)
