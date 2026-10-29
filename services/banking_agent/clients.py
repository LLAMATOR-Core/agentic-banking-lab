from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

import httpx

from services.common.auth import ServiceTokenProvider
from services.common.llm import raise_upstream_llm_error


class MemoryClient:
    def __init__(self, tokens: ServiceTokenProvider) -> None:
        self.base_url = os.getenv("MEMORY_URL", "http://memory-service:8002")
        self.tokens = tokens
        try:
            # Recall invokes the configured embedding provider.
            self.timeout = float(os.getenv("MEMORY_HTTP_TIMEOUT_SECONDS", "60"))
        except ValueError:
            self.timeout = 60.0

    async def recall(
        self, user_id: str, query: str, conversation_id: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        payload = {
            "agent_id": "banking-agent",
            "user_id": user_id,
            "query": query,
            "conversation_id": conversation_id,
            "limit_per_layer": max(1, min(limit, 20)),
        }
        result = await self._request("POST", "/api/v1/context", json=payload)
        return result["memories"]

    async def create_action(
        self,
        *,
        user_id: str,
        conversation_id: str,
        action_type: str,
        parameters: dict[str, Any],
        expires_at: datetime,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/working/actions",
            json={
                "agent_id": "banking-agent",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "action_type": action_type,
                "parameters": parameters,
                "expires_at": expires_at.isoformat(),
            },
        )

    async def stage_candidate(
        self,
        *,
        user_id: str,
        conversation_id: str,
        requested_memory_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/working/candidates",
            json={
                "agent_id": "banking-agent",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "requested_memory_type": requested_memory_type,
                "content": content,
                "metadata": metadata,
            },
        )

    async def store_turn(
        self,
        *,
        user_id: str,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        intent: str,
        tool_calls: list[str],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/working/turns",
            json={
                "agent_id": "banking-agent",
                "user_id": user_id,
                "conversation_id": conversation_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "intent": intent,
                "tool_calls": tool_calls,
            },
        )

    async def conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            f"/api/v1/working/conversations/banking-agent/{user_id}",
            params={"limit": limit},
        )
        return result["conversations"]

    async def conversation_messages(
        self, user_id: str, conversation_id: str
    ) -> list[dict[str, Any]]:
        result = await self._request(
            "GET",
            f"/api/v1/working/banking-agent/{user_id}/{conversation_id}",
        )
        return result["messages"]

    async def get_action(self, memory_id: str, user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/working/actions/{memory_id}",
            params={"agent_id": "banking-agent", "user_id": user_id},
        )

    async def patch_action(
        self, memory_id: str, user_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/api/v1/working/actions/{memory_id}",
            json={
                "agent_id": "banking-agent",
                "user_id": user_id,
                "metadata": metadata,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self.tokens.get()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            raise_upstream_llm_error(response)
            response.raise_for_status()
            return response.json()


class TravelA2AClient:
    def __init__(self, tokens: ServiceTokenProvider) -> None:
        self.base_url = os.getenv("TRAVEL_AGENT_URL", "http://travel-agent:8003")
        self.tokens = tokens
        try:
            self.timeout = float(os.getenv("TRAVEL_HTTP_TIMEOUT_SECONDS", "180"))
        except ValueError:
            self.timeout = 180.0

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
        async with httpx.AsyncClient(timeout=self.timeout) as client:
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
            raise_upstream_llm_error(response)
            response.raise_for_status()
        body = response.json()
        return body["message"]["parts"][0]["data"]
