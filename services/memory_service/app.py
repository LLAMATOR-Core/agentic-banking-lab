from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument

from services.common.auth import Principal, require_principal
from services.common.tracing import observation

MemoryType = Literal["semantic", "procedural", "episodic", "working"]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    memory_type: MemoryType
    content: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    user_id: str
    query: str = Field(default="", max_length=4000)
    memory_types: list[MemoryType] = Field(
        default_factory=lambda: ["semantic", "procedural", "episodic", "working"]
    )
    conversation_id: str | None = None
    limit: int = Field(default=8, ge=1, le=30)


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, Any]


mongo_client: AsyncMongoClient | None = None
memories = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global mongo_client, memories
    mongo_client = AsyncMongoClient(
        os.getenv("MONGODB_URL", "mongodb://root:mongo@localhost:27017/?authSource=admin")
    )
    database = mongo_client[os.getenv("MONGODB_DATABASE", "agent_memory")]
    memories = database["memories"]
    await memories.create_index(
        [
            ("agent_id", ASCENDING),
            ("user_id", ASCENDING),
            ("memory_type", ASCENDING),
            ("created_at", DESCENDING),
        ]
    )
    await memories.create_index("expires_at", expireAfterSeconds=0)
    await memories.create_index([("content", "text")], default_language="none")
    await _seed_agent_knowledge()
    yield
    await mongo_client.close()


app = FastAPI(title="Agent Memory Orchestrator", version="0.1.0", lifespan=lifespan)
Caller = Annotated[Principal, Depends(require_principal)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "memory-orchestrator"}


@app.post("/v1/memories")
async def create_memory(payload: MemoryCreate, caller: Caller) -> dict[str, Any]:
    _ensure_ready()
    now = datetime.now(UTC)
    document = {
        **payload.model_dump(),
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
        "created_by": caller.authorized_party,
    }
    with observation(
        "memory.write",
        input={"agent_id": payload.agent_id, "memory_type": payload.memory_type},
        metadata={"user_id": payload.user_id},
    ) as span:
        result = await memories.insert_one(document)
        span.update(output={"memory_id": str(result.inserted_id)})
    return {"id": str(result.inserted_id), **_serialize(document)}


@app.get("/v1/memories/{memory_id}")
async def get_memory(memory_id: str, _: Caller) -> dict[str, Any]:
    _ensure_ready()
    object_id = _object_id(memory_id)
    document = await memories.find_one({"_id": object_id})
    if not document:
        raise HTTPException(404, "Memory not found")
    return _serialize(document)


@app.patch("/v1/memories/{memory_id}")
async def patch_memory(memory_id: str, payload: MetadataPatch, _: Caller) -> dict[str, Any]:
    _ensure_ready()
    object_id = _object_id(memory_id)
    updates = {f"metadata.{key}": value for key, value in payload.metadata.items()}
    updates["updated_at"] = datetime.now(UTC)
    document = await memories.find_one_and_update(
        {"_id": object_id}, {"$set": updates}, return_document=ReturnDocument.AFTER
    )
    if not document:
        raise HTTPException(404, "Memory not found")
    return _serialize(document)


@app.post("/v1/recall")
async def recall(payload: RecallRequest, _: Caller) -> dict[str, Any]:
    _ensure_ready()
    selector: dict[str, Any] = {
        "agent_id": payload.agent_id,
        "user_id": {"$in": [payload.user_id, "*"]},
        "memory_type": {"$in": payload.memory_types},
        "$or": [{"expires_at": None}, {"expires_at": {"$gt": datetime.now(UTC)}}],
    }
    if payload.conversation_id:
        selector["$and"] = [
            {
                "$or": [
                    {"conversation_id": payload.conversation_id},
                    {"conversation_id": None},
                ]
            }
        ]
    with observation(
        "memory.recall",
        input={"query": payload.query, "types": payload.memory_types},
        metadata={"agent_id": payload.agent_id, "user_id": payload.user_id},
    ) as span:
        candidates = (
            await memories.find(selector).sort("created_at", DESCENDING).limit(200).to_list()
        )
        query_tokens = _tokens(payload.query)
        ranked = sorted(
            candidates,
            key=lambda item: _score(item, query_tokens, payload.conversation_id),
            reverse=True,
        )[: payload.limit]
        span.update(output={"matches": len(ranked)})
    return {"memories": [_serialize(item) for item in ranked]}


@app.get("/v1/memories")
async def list_memories(
    _: Caller,
    agent_id: str = Query(max_length=128),
    user_id: str = Query(max_length=128),
    memory_type: MemoryType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    _ensure_ready()
    selector: dict[str, Any] = {"agent_id": agent_id, "user_id": user_id}
    if memory_type:
        selector["memory_type"] = memory_type
    rows = await memories.find(selector).sort("created_at", DESCENDING).limit(limit).to_list()
    return {"memories": [_serialize(item) for item in rows]}


async def _seed_agent_knowledge() -> None:
    seeds = [
        (
            "semantic",
            "АБС — источник истины по клиенту, счетам, операциям и инвестиционным позициям. "
            "Идентификатор клиента нельзя брать из текста пользователя.",
            {"source": "stand-bootstrap", "topic": "banking-environment"},
        ),
        (
            "procedural",
            "Любая операция списания выполняется только после отдельного явного подтверждения. "
            "На подтверждении необходимо повторно проверить agent_id, user_id, "
            "статус и параметры операции.",
            {"source": "stand-bootstrap", "topic": "transaction-safety"},
        ),
        (
            "procedural",
            "Для поездки банковский агент передаёт тревел-агенту только маршрут, "
            "даты, число пассажиров, "
            "интересы и явно заданный бюджет; номера счетов и балансы не передаются.",
            {"source": "stand-bootstrap", "topic": "data-minimization"},
        ),
    ]
    for memory_type, content, metadata in seeds:
        await memories.update_one(
            {
                "agent_id": "banking-agent",
                "user_id": "*",
                "memory_type": memory_type,
                "content": content,
            },
            {
                "$setOnInsert": {
                    "conversation_id": None,
                    "metadata": metadata,
                    "expires_at": None,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "schema_version": 1,
                    "created_by": "bootstrap",
                }
            },
            upsert=True,
        )


def _score(document: dict[str, Any], query_tokens: set[str], conversation_id: str | None) -> float:
    content_tokens = _tokens(document.get("content", ""))
    overlap = len(query_tokens & content_tokens) / max(len(query_tokens), 1)
    type_boost = {"procedural": 0.25, "semantic": 0.2, "episodic": 0.15, "working": 0.05}[
        document["memory_type"]
    ]
    conversation_boost = (
        0.4 if conversation_id and document.get("conversation_id") == conversation_id else 0
    )
    return overlap + type_boost + conversation_boost


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[\wа-яё]+", text.lower()) if len(token) > 2}


def _serialize(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    if "_id" in result:
        result["id"] = str(result.pop("_id"))
    for key in ("created_at", "updated_at", "expires_at"):
        if isinstance(result.get(key), datetime):
            value = result[key]
            # BSON stores UTC datetimes without a timezone offset. Keep the
            # API contract explicit so downstream agents never mix naive and
            # timezone-aware values.
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            result[key] = value.isoformat()
    return result


def _object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(400, "Invalid memory id") from exc


def _ensure_ready() -> None:
    if memories is None:
        raise HTTPException(503, "Memory storage is not ready")
