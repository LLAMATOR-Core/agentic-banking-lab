from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from services.common.auth import Principal, require_principal
from services.common.llm import install_llm_error_handlers
from services.common.tracing import observation
from services.memory_service.core import (
    EmbeddingClient,
    MemoryStore,
    MemoryType,
    object_id,
    serialize,
)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=128)
    memory_types: list[MemoryType] = Field(
        default_factory=lambda: ["episodic", "semantic", "procedural", "working"]
    )
    limit_per_layer: int = Field(default=4, ge=1, le=20)


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=3, max_length=128)
    user_message: str = Field(min_length=1, max_length=4000)
    assistant_message: str = Field(min_length=1, max_length=4000)
    intent: str = Field(min_length=1, max_length=64)
    tool_calls: list[str] = Field(default_factory=list, max_length=30)


class WorkingActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=3, max_length=128)
    action_type: str = Field(min_length=2, max_length=64)
    parameters: dict[str, Any]
    expires_at: datetime


class WorkingActionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    metadata: dict[str, Any]


class WorkingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=2, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=3, max_length=128)
    content: str = Field(min_length=1, max_length=12000)
    requested_memory_type: Literal["semantic", "procedural", "episodic"]
    metadata: dict[str, Any] = Field(default_factory=dict)


store = MemoryStore()
embeddings = EmbeddingClient("memory-service")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.connect()
    yield
    await store.close()


app = FastAPI(title="Agent Memory Data Service", version="0.2.0", lifespan=lifespan)
install_llm_error_handlers(app)
Caller = Annotated[Principal, Depends(require_principal)]


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "memory-service",
        "mode": "working-write-and-read-only-long-term",
        "embeddings": embeddings.public_config(),
    }


@app.post("/api/v1/context")
async def enrich_context(payload: ContextRequest, _: Caller) -> dict[str, Any]:
    """Read-only context enrichment for agents; no long-term mutation is possible here."""
    store.ensure_ready()
    vector = await embeddings.embed(payload.query)
    layers: dict[str, list[dict[str, Any]]] = {}
    with observation(
        "memory.context",
        input={"types": payload.memory_types, "query_chars": len(payload.query)},
        user_id=payload.user_id,
        session_id=payload.conversation_id,
        agent_id=payload.agent_id,
    ) as span:
        for memory_type in ("episodic", "semantic", "procedural"):
            if memory_type not in payload.memory_types:
                continue
            layers[memory_type] = await store.search(
                memory_type=memory_type,
                agent_id=payload.agent_id,
                user_id=payload.user_id,
                query_vector=vector,
                limit=payload.limit_per_layer,
            )
        if "working" in payload.memory_types:
            selector: dict[str, Any] = {
                "agent_id": payload.agent_id,
                "user_id": payload.user_id,
                "memory_type": "working",
                "metadata.message_role": {"$in": ["user", "assistant", "external-source"]},
            }
            if payload.conversation_id:
                selector["conversation_id"] = payload.conversation_id
            rows = (
                await store.memories.find(selector)
                .sort("created_at", DESCENDING)
                .limit(payload.limit_per_layer * 2)
                .to_list()
            )
            layers["working"] = [serialize(row) for row in reversed(rows)]
        flattened = [
            item for memory_type in payload.memory_types for item in layers.get(memory_type, [])
        ]
        span.update(output={key: len(value) for key, value in layers.items()})
    return {"layers": layers, "memories": flattened, "embedding_model": embeddings.model}


async def _search_layer(
    memory_type: str,
    agent_id: str,
    user_id: str | None,
    query: str,
    limit: int,
) -> dict[str, Any]:
    vector = await embeddings.embed(query)
    rows = await store.search(
        memory_type=memory_type,
        agent_id=agent_id,
        user_id=user_id,
        query_vector=vector,
        limit=limit,
    )
    return {"memory_type": memory_type, "results": rows, "embedding_model": embeddings.model}


@app.get("/api/v1/episodic/{agent_id}/{user_id}")
async def search_episodic(
    agent_id: str,
    user_id: str,
    _: Caller,
    query: str = Query(min_length=1, max_length=4000),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    return await _search_layer("episodic", agent_id, user_id, query, limit)


@app.get("/api/v1/episodic/{agent_id}/{user_id}/{episode_id}")
async def get_episode(agent_id: str, user_id: str, episode_id: str, _: Caller) -> dict[str, Any]:
    return await _get_scoped(agent_id, user_id, "episodic", episode_id)


@app.get("/api/v1/semantic/{agent_id}/{user_id}")
async def search_semantic(
    agent_id: str,
    user_id: str,
    _: Caller,
    query: str = Query(min_length=1, max_length=4000),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    return await _search_layer("semantic", agent_id, user_id, query, limit)


@app.get("/api/v1/semantic/{agent_id}/{user_id}/{semantic_id}")
async def get_semantic(
    agent_id: str, user_id: str, semantic_id: str, _: Caller
) -> dict[str, Any]:
    return await _get_scoped(agent_id, user_id, "semantic", semantic_id)


@app.get("/api/v1/procedure/{agent_id}")
async def search_procedures(
    agent_id: str,
    _: Caller,
    query: str = Query(min_length=1, max_length=4000),
    user_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    return await _search_layer("procedural", agent_id, user_id, query, limit)


@app.get("/api/v1/procedure/{agent_id}/{procedure_id}")
async def get_procedure(agent_id: str, procedure_id: str, _: Caller) -> dict[str, Any]:
    store.ensure_ready()
    try:
        identifier = object_id(procedure_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    document = await store.memories.find_one(
        {"_id": identifier, "agent_id": agent_id, "memory_type": "procedural"}
    )
    if not document:
        raise HTTPException(404, "Procedure not found")
    return serialize(document)


@app.post("/api/v1/working/turns", status_code=202)
async def store_conversation_turn(payload: ConversationTurn, caller: Caller) -> dict[str, Any]:
    """Persist the current dialogue and queue it; no long-term memory is written here."""
    store.ensure_ready()
    now = datetime.now(UTC)
    turn_id = str(uuid.uuid4())
    title = _conversation_title(payload.user_message)
    common = {
        "source": "agent-chat",
        "turn_id": turn_id,
        "intent": payload.intent,
        "tool_calls": payload.tool_calls,
        "conversation_title": title,
        "migration_status": "pending",
    }
    documents = [
        _working_document(payload, payload.user_message, "user", 0, common, now, caller),
        _working_document(payload, payload.assistant_message, "assistant", 1, common, now, caller),
    ]
    result = await store.memories.insert_many(documents)
    job = await store.jobs.insert_one(
        {
            "kind": "turn",
            "status": "pending",
            "attempts": 0,
            "agent_id": payload.agent_id,
            "user_id": payload.user_id,
            "conversation_id": payload.conversation_id,
            "turn_id": turn_id,
            "user_message": payload.user_message,
            "assistant_message": payload.assistant_message,
            "intent": payload.intent,
            "created_at": now,
            "updated_at": now,
        }
    )
    return {
        "turn_id": turn_id,
        "messages": len(result.inserted_ids),
        "migration": {"status": "pending", "job_id": str(job.inserted_id)},
    }


@app.post("/api/v1/working/candidates", status_code=202)
async def stage_candidate(payload: WorkingCandidate, caller: Caller) -> dict[str, Any]:
    """Stage untrusted content in working memory for the next orchestrator collection."""
    store.ensure_ready()
    now = datetime.now(UTC)
    candidate_id = str(uuid.uuid4())
    document = {
        "agent_id": payload.agent_id,
        "user_id": payload.user_id,
        "memory_type": "working",
        "content": payload.content,
        "payload": None,
        "conversation_id": payload.conversation_id,
        "metadata": {
            **payload.metadata,
            "message_role": "external-source",
            "candidate_id": candidate_id,
            "requested_memory_type": payload.requested_memory_type,
            "migration_status": "pending",
        },
        "embedding": None,
        "embedding_model": None,
        "expires_at": None,
        "created_at": now,
        "updated_at": now,
        "schema_version": 2,
        "created_by": caller.authorized_party,
    }
    inserted = await store.memories.insert_one(document)
    job = await store.jobs.insert_one(
        {
            "kind": "candidate",
            "status": "pending",
            "attempts": 0,
            "agent_id": payload.agent_id,
            "user_id": payload.user_id,
            "conversation_id": payload.conversation_id,
            "candidate_id": candidate_id,
            "content": payload.content,
            "requested_memory_type": payload.requested_memory_type,
            "source_metadata": payload.metadata,
            "created_at": now,
            "updated_at": now,
        }
    )
    return {
        "working_memory_id": str(inserted.inserted_id),
        "candidate_id": candidate_id,
        "migration": {"status": "pending", "job_id": str(job.inserted_id)},
    }


@app.get("/api/v1/working/conversations/{agent_id}/{user_id}")
async def list_conversations(
    agent_id: str,
    user_id: str,
    _: Caller,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    store.ensure_ready()
    pipeline = [
        {
            "$match": {
                "agent_id": agent_id,
                "user_id": user_id,
                "memory_type": "working",
                "conversation_id": {"$ne": None},
                "metadata.message_role": {"$in": ["user", "assistant"]},
            }
        },
        {"$sort": {"created_at": 1}},
        {
            "$group": {
                "_id": "$conversation_id",
                "title": {"$first": "$metadata.conversation_title"},
                "created_at": {"$first": "$created_at"},
                "updated_at": {"$last": "$created_at"},
                "message_count": {"$sum": 1},
            }
        },
        {"$sort": {"updated_at": -1}},
        {"$limit": limit},
    ]
    rows = await (await store.memories.aggregate(pipeline)).to_list()
    return {
        "conversations": [
            {
                "conversation_id": row["_id"],
                "title": row.get("title") or "Диалог",
                "created_at": _isoformat(row["created_at"]),
                "updated_at": _isoformat(row["updated_at"]),
                "message_count": row["message_count"],
            }
            for row in rows
        ]
    }


@app.get("/api/v1/working/{agent_id}/{user_id}/{conversation_id}")
async def conversation_messages(
    agent_id: str, user_id: str, conversation_id: str, _: Caller
) -> dict[str, Any]:
    store.ensure_ready()
    rows = await (
        store.memories.find(
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "memory_type": "working",
                "conversation_id": conversation_id,
                "metadata.message_role": {"$in": ["user", "assistant"]},
            }
        )
        .sort([("created_at", ASCENDING), ("metadata.message_index", ASCENDING)])
        .to_list()
    )
    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": row["metadata"]["message_role"],
                "content": row["content"],
                "created_at": _isoformat(row["created_at"]),
            }
            for row in rows
        ],
    }


@app.post("/api/v1/working/actions")
async def create_action(payload: WorkingActionCreate, caller: Caller) -> dict[str, Any]:
    store.ensure_ready()
    now = datetime.now(UTC)
    document = {
        "agent_id": payload.agent_id,
        "user_id": payload.user_id,
        "memory_type": "working",
        "content": f"Pending financial action: {payload.action_type}",
        "payload": None,
        "conversation_id": payload.conversation_id,
        "metadata": {
            "record_kind": "pending-action",
            "status": "pending",
            "action_type": payload.action_type,
            "parameters": payload.parameters,
        },
        "embedding": None,
        "embedding_model": None,
        "expires_at": payload.expires_at,
        "created_at": now,
        "updated_at": now,
        "schema_version": 2,
        "created_by": caller.authorized_party,
    }
    result = await store.memories.insert_one(document)
    return {"id": str(result.inserted_id), **serialize(document)}


@app.get("/api/v1/working/actions/{action_id}")
async def get_action(
    action_id: str,
    _: Caller,
    agent_id: str = Query(min_length=2, max_length=128),
    user_id: str = Query(min_length=1, max_length=128),
) -> dict[str, Any]:
    return await _get_scoped(agent_id, user_id, "working", action_id, "pending-action")


@app.patch("/api/v1/working/actions/{action_id}")
async def patch_action(action_id: str, payload: WorkingActionPatch, _: Caller) -> dict[str, Any]:
    store.ensure_ready()
    try:
        identifier = object_id(action_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    updates = {f"metadata.{key}": value for key, value in payload.metadata.items()}
    updates["updated_at"] = datetime.now(UTC)
    document = await store.memories.find_one_and_update(
        {
            "_id": identifier,
            "agent_id": payload.agent_id,
            "user_id": payload.user_id,
            "memory_type": "working",
            "metadata.record_kind": "pending-action",
        },
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not document:
        raise HTTPException(404, "Pending action not found")
    return serialize(document)


async def _get_scoped(
    agent_id: str,
    user_id: str,
    memory_type: str,
    memory_id: str,
    record_kind: str | None = None,
) -> dict[str, Any]:
    store.ensure_ready()
    try:
        identifier = object_id(memory_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    selector: dict[str, Any] = {
        "_id": identifier,
        "agent_id": agent_id,
        "user_id": {"$in": [user_id, "*"]},
        "memory_type": memory_type,
    }
    if record_kind:
        selector["metadata.record_kind"] = record_kind
    document = await store.memories.find_one(selector)
    if not document:
        raise HTTPException(404, "Memory record not found")
    return serialize(document)


def _working_document(
    payload: ConversationTurn,
    content: str,
    role: str,
    index: int,
    common: dict[str, Any],
    now: datetime,
    caller: Principal,
) -> dict[str, Any]:
    return {
        "agent_id": payload.agent_id,
        "user_id": payload.user_id,
        "memory_type": "working",
        "content": content,
        "payload": None,
        "conversation_id": payload.conversation_id,
        "metadata": {**common, "message_role": role, "message_index": index},
        "embedding": None,
        "embedding_model": None,
        "expires_at": None,
        "created_at": now,
        "updated_at": now,
        "schema_version": 2,
        "created_by": caller.authorized_party,
    }


def _conversation_title(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:72] + ("…" if len(compact) > 72 else "")


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
