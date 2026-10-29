from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from bson import ObjectId
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from services.common.llm import LLMServiceError
from services.common.tracing import observation

MemoryType = Literal["semantic", "procedural", "episodic", "working"]


class EmbeddingClient:
    """OpenAI-compatible embeddings client shared by memory read and write planes."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        self.model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip()
        try:
            timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        except ValueError:
            timeout = 45.0
        self.client = AsyncOpenAI(
            api_key=self.api_key or "not-configured",
            base_url=self.base_url or "https://api.openai.com/v1",
            timeout=timeout,
            max_retries=0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def public_config(self) -> dict[str, Any]:
        parsed = urlsplit(self.base_url)
        return {
            "required": True,
            "configured": self.configured,
            "model": self.model or "not-configured",
            "provider": parsed.netloc or parsed.path or "not-configured",
        }

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise LLMServiceError(
                "Embedding-модель недоступна: задайте OPENAI_API_KEY, OPENAI_BASE_URL "
                "и EMBEDDING_MODEL"
            )
        compact = [" ".join(text.split())[:30000] for text in texts]
        if not compact or any(not text for text in compact):
            raise ValueError("Embedding input must not be empty")
        with observation(
            f"embedding.{self.component}",
            input={"items": len(compact), "characters": sum(len(text) for text in compact)},
            metadata={"component": self.component, "model": self.model},
            agent_id=self.component,
        ) as span:
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=compact,
                    encoding_format="float",
                )
            except (APIConnectionError, APITimeoutError) as exc:
                raise LLMServiceError(
                    "Embedding-модель недоступна: не удалось подключиться к провайдеру"
                ) from exc
            except APIStatusError as exc:
                raise LLMServiceError(
                    f"Embedding-модель недоступна: провайдер вернул HTTP {exc.status_code}"
                ) from exc
            except OpenAIError as exc:
                raise LLMServiceError("Embedding-модель недоступна: ошибка провайдера") from exc
            if len(response.data) != len(compact) or any(
                not item.embedding for item in response.data
            ):
                raise LLMServiceError("Embedding-модель не вернула вектор", status_code=502)
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors = [[float(value) for value in item.embedding] for item in ordered]
            span.update(output={"items": len(vectors), "dimensions": len(vectors[0])})
            return vectors


class MemoryStore:
    def __init__(self, *, recover_jobs: bool = False) -> None:
        self.recover_jobs = recover_jobs
        self.client: AsyncMongoClient | None = None
        self.memories = None
        self.jobs = None

    async def connect(self) -> None:
        self.client = AsyncMongoClient(
            os.getenv("MONGODB_URL", "mongodb://root:mongo@localhost:27017/?authSource=admin")
        )
        database = self.client[os.getenv("MONGODB_DATABASE", "agent_memory")]
        self.memories = database["memories"]
        self.jobs = database["memory_jobs"]
        await self.memories.create_index(
            [
                ("agent_id", ASCENDING),
                ("user_id", ASCENDING),
                ("memory_type", ASCENDING),
                ("created_at", DESCENDING),
            ]
        )
        await self.memories.create_index("expires_at", expireAfterSeconds=0)
        await self.memories.create_index(
            [("agent_id", ASCENDING), ("user_id", ASCENDING), ("conversation_id", ASCENDING)]
        )
        await self.jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.jobs.create_index(
            [("agent_id", ASCENDING), ("user_id", ASCENDING), ("conversation_id", ASCENDING)]
        )
        if self.recover_jobs:
            await self.jobs.update_many(
                {"status": "processing"},
                {"$set": {"status": "pending", "updated_at": datetime.now(UTC)}},
            )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    def ensure_ready(self) -> None:
        if self.memories is None or self.jobs is None:
            raise RuntimeError("Memory storage is not ready")

    async def search(
        self,
        *,
        memory_type: str,
        agent_id: str,
        user_id: str | None,
        query_vector: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        self.ensure_ready()
        selector: dict[str, Any] = {
            "agent_id": agent_id,
            "memory_type": memory_type,
            "embedding": {"$type": "array"},
            "$or": [{"expires_at": None}, {"expires_at": {"$gt": datetime.now(UTC)}}],
        }
        if memory_type == "procedural":
            selector["user_id"] = "*" if user_id is None else {"$in": [user_id, "*"]}
        elif user_id is not None:
            selector["user_id"] = {"$in": [user_id, "*"]}
        rows = (
            await self.memories.find(selector)
            .sort("created_at", DESCENDING)
            .limit(500)
            .to_list()
        )
        scored = [
            (cosine_similarity(query_vector, row.get("embedding") or []), row) for row in rows
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {**serialize(row), "similarity": round(score, 6)}
            for score, row in scored[:limit]
            if score > -1
        ]

    async def insert_long_term(
        self,
        *,
        agent_id: str,
        user_id: str,
        memory_type: str,
        content: str,
        embedding: list[float],
        embedding_model: str,
        metadata: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        created_by: str,
    ) -> dict[str, Any]:
        self.ensure_ready()
        now = datetime.now(UTC)
        document = {
            "agent_id": agent_id,
            "user_id": user_id,
            "memory_type": memory_type,
            "content": content,
            "payload": payload,
            "conversation_id": None,
            "metadata": metadata or {},
            "embedding": embedding,
            "embedding_model": embedding_model,
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
            "schema_version": 2,
            "created_by": created_by,
        }
        result = await self.memories.insert_one(document)
        return {"id": str(result.inserted_id), **serialize(document)}


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return -1.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return numerator / (left_norm * right_norm)


def serialize(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    if "_id" in result:
        result["id"] = str(result.pop("_id"))
    # Vectors are internal retrieval data and must not inflate agent context or API responses.
    result.pop("embedding", None)
    for key in ("created_at", "updated_at", "expires_at"):
        if isinstance(result.get(key), datetime):
            value = result[key]
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            result[key] = value.isoformat()
    return result


def object_id(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise ValueError("Invalid memory id")
    return ObjectId(value)
