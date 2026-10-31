from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from services.banking_agent.clients import MemoryClient
from services.banking_agent.context import time_context
from services.banking_agent.graph import BankingGraph
from services.banking_agent.skills import BankingSkills
from services.common.llm import LLMClient


class ContextMemory:
    def __init__(self):
        self.history = [
            {
                "role": role,
                "content": f"turn-{index}-{role}: " + "x" * 3200,
                "created_at": f"2026-10-31T12:{index:02}:00+00:00",
            }
            for index in range(20)
            for role in ("user", "assistant")
        ]
        self.memories = [
            {
                "id": f"{layer}-{index}",
                "memory_type": layer,
                "content": f"{layer}-{index}: " + "m" * 2000 + " END-OF-MEMORY",
                "metadata": {"source": "test-source", "trust": "untrusted-external"},
            }
            for layer in ("episodic", "semantic", "procedural")
            for index in range(8)
        ]
        self.calls = []
        self.turns = []

    async def recall(self, user_id, query, conversation_id):
        self.calls.append(("recall", user_id, conversation_id))
        return self.memories

    async def conversation_messages(self, user_id, conversation_id):
        self.calls.append(("history", user_id, conversation_id))
        return self.history

    async def store_turn(self, **kwargs):
        self.turns.append(kwargs)


@pytest.mark.asyncio
async def test_every_banking_llm_request_receives_complete_context_at_http_boundary(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("BANK_TIMEZONE", "Europe/Moscow")
    snapshot = time_context(datetime(2026, 10, 31, 22, 30, tzinfo=UTC))
    monkeypatch.setattr("services.banking_agent.graph.time_context", lambda: snapshot)
    requests = []
    observations = []

    @contextmanager
    def observe(name, **kwargs):
        observations.append(kwargs)
        yield SimpleNamespace(update=lambda **updates: None)

    monkeypatch.setattr("services.common.llm.observation", observe)
    outputs = [
        {"intent": "travel", "rationale": "continue the previous trip"},
        {
            "request_type": "trip_search",
            "origin": "MOW",
            "destination": "Стамбул",
            "destination_airport": "IST",
            "departure_date": "2026-11-08",
            "return_date": "2026-11-15",
            "adults": 1,
            "currency": "RUB",
        },
        {"message": "Иван Сергеевич, вот варианты поездки."},
    ]

    def complete(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(outputs[len(requests) - 1]),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    llm = LLMClient("banking-agent")
    await llm.client.close()
    llm.client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(complete)),
    )
    memory = ContextMemory()
    mcp_calls = []
    partner_calls = []

    async def customer(name, arguments, token):
        mcp_calls.append((name, arguments, token))
        return {
            "id": "customer-001",
            "full_name": "Петров Иван Сергеевич",
            "gender": "male",
            "client_type": "retail",
            "vip": True,
            "email": "PRIVATE_EMAIL_CANARY",
            "phone": "PRIVATE_PHONE_CANARY",
        }

    async def plan(request, conversation_id):
        partner_calls.append(request)
        return {"agent_summary": "Найдены варианты", "flights": [{"price": 12345}]}

    skills = BankingSkills(memory, SimpleNamespace(plan=plan), llm=llm)
    skills.mcp = SimpleNamespace(call=customer)
    graph = BankingGraph(memory, skills, llm)
    try:
        result = await graph.graph.ainvoke(
            {
                "message": "А теперь на следующей неделе",
                "user_id": "customer-001",
                "conversation_id": "session-current",
                "access_token": "USER_JWT_CANARY",
            }
        )
    finally:
        await llm.client.close()

    assert len(requests) == 3  # Routing, travel extraction, answer composition.
    assert all(item["metadata"]["conversation_messages"] == 40 for item in observations)
    assert all(item["metadata"]["memory_records"] == 24 for item in observations)
    assert all(
        item["metadata"]["memory_layers"]
        == {
            "episodic": 8,
            "semantic": 8,
            "procedural": 8,
        }
        for item in observations
    )
    for request in requests:
        payload = json.loads(request["messages"][1]["content"])
        assert payload["current_date"] == "2026-11-01"  # Moscow, not the UTC date.
        assert payload["current_datetime"] == snapshot["current_datetime"]
        assert payload["timezone"] == "Europe/Moscow"
        assert payload["customer_profile"] == {
            "full_name": "Петров Иван Сергеевич",
            "gender": "male",
            "client_type": "retail",
            "vip": True,
        }
        assert payload["conversation_history"] == memory.history  # All 40 messages, >16/1500 chars.
        assert payload["user_message"] == "А теперь на следующей неделе"
        assert len(payload["memory_context"]) == 24
        assert [item["content"] for item in payload["memory_context"]] == [
            item["content"] for item in memory.memories
        ]
        assert {item["memory_type"] for item in payload["memory_context"]} == {
            "episodic",
            "semantic",
            "procedural",
        }
        assert all(item["trust"] == "untrusted-external" for item in payload["memory_context"])
    wire = json.dumps(requests, ensure_ascii=False)
    assert "USER_JWT_CANARY" not in wire
    assert "PRIVATE_EMAIL_CANARY" not in wire and "PRIVATE_PHONE_CANARY" not in wire
    assert mcp_calls == [("get_customer_info", {}, "USER_JWT_CANARY")]
    assert memory.calls == [
        ("recall", "customer-001", "session-current"),
        ("history", "customer-001", "session-current"),
    ]
    partner_wire = json.dumps(partner_calls, ensure_ascii=False)
    assert "Иван" not in partner_wire and "memory_context" not in partner_wire
    assert "conversation_history" not in partner_wire
    assert json.loads(requests[-1]["messages"][1]["content"])["skill_data"]["travel"]["flights"]
    assert memory.turns[0]["assistant_message"] == result["response"]


@pytest.mark.asyncio
async def test_recall_selects_each_long_term_layer_without_mixing_in_recent_chat(monkeypatch):
    client = MemoryClient(SimpleNamespace())
    calls = []

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"memories": [{"content": "selected memory"}]}

    monkeypatch.setattr(client, "_request", request)
    assert await client.recall("customer-002", "query", "session-2") == [
        {"content": "selected memory"}
    ]
    assert calls == [
        (
            "POST",
            "/api/v1/context",
            {
                "json": {
                    "agent_id": "banking-agent",
                    "user_id": "customer-002",
                    "query": "query",
                    "conversation_id": "session-2",
                    "memory_types": ["episodic", "semantic", "procedural"],
                    "limit_per_layer": 8,
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_customer_profile_is_refreshed_per_user_and_cannot_mismatch_identity():
    memory = ContextMemory()

    async def profile(token):
        return {"id": token, "full_name": f"Name for {token}"}

    graph = object.__new__(BankingGraph)
    graph.memory = memory
    graph.skills = SimpleNamespace(load_customer_profile=profile)
    for user_id in ("customer-001", "customer-002"):
        state = await graph.recall_memory(
            {
                "user_id": user_id,
                "access_token": user_id,
                "message": "hello",
                "conversation_id": f"session-{user_id}",
            }
        )
        assert state["customer_profile"]["id"] == user_id
        assert memory.calls[-1] == ("history", user_id, f"session-{user_id}")
    with pytest.raises(ValueError, match="authenticated user"):
        await graph.recall_memory(
            {
                "user_id": "customer-002",
                "access_token": "customer-001",
                "message": "hello",
                "conversation_id": "session-2",
            }
        )


@pytest.mark.asyncio
async def test_history_read_is_scoped_to_one_user_and_session_without_a_recent_message_limit(
    monkeypatch,
):
    from services.memory_service import app as memory_service

    memory = ContextMemory()
    selectors = []
    ordering = []

    class Cursor:
        def sort(self, order):
            ordering.append(order)
            return self

        async def to_list(self):
            return [
                {
                    "metadata": {"message_role": row["role"]},
                    "content": row["content"],
                    "created_at": datetime.fromisoformat(row["created_at"]),
                }
                for row in memory.history
            ]

    def find(selector):
        selectors.append(selector)
        return Cursor()

    monkeypatch.setattr(memory_service.store, "memories", SimpleNamespace(find=find))
    monkeypatch.setattr(memory_service.store, "ensure_ready", lambda: None)
    result = await memory_service.conversation_messages(
        "banking-agent",
        "customer-001",
        "session-current",
        SimpleNamespace(),
    )
    assert result["messages"] == memory.history
    assert selectors == [
        {
            "agent_id": "banking-agent",
            "user_id": "customer-001",
            "memory_type": "working",
            "conversation_id": "session-current",
            "metadata.message_role": {"$in": ["user", "assistant"]},
        }
    ]
    assert ordering == [[("created_at", 1), ("metadata.message_index", 1)]]
