from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import get_current_span
from pydantic import BaseModel

from services.common import tracing
from services.common.llm import LLMClient, LLMResponseError
from services.common.mcp import MCPClient, MCPTool, create_mcp_app


@pytest.fixture
def recorded(monkeypatch):
    """Use real OTel parents with a local recording Langfuse client, no collector."""
    rows = []
    provider = TracerProvider()
    tracer = provider.get_tracer("payload-contracts")

    @contextmanager
    def start(**attributes):
        with tracer.start_as_current_span(attributes["name"]) as otel_span:
            row = {
                **attributes,
                "id": otel_span.get_span_context().span_id,
                "trace_id": otel_span.get_span_context().trace_id,
                "parent_id": otel_span.parent.span_id if otel_span.parent else None,
            }
            rows.append(row)
            yield SimpleNamespace(update=lambda **updates: row.update(updates))

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-local-test")
    monkeypatch.setenv("AGENT_SYSTEM_ID", "banking")
    monkeypatch.setattr(
        tracing,
        "get_client",
        lambda: SimpleNamespace(
            start_as_current_observation=start,
        ),
    )
    yield rows
    provider.shutdown()


def test_payloads_are_complete_snapshots_with_recursive_secret_redaction(recorded):
    long_text = "memory evidence " * 3000 + "END-OF-EVIDENCE"
    payload = {
        "nested": [{"access_token": "USER_JWT", "content": long_text}],
        "message": json.dumps({"authorization": "Bearer PRIVATE", "fact": "keep"}),
    }
    with tracing.observation("snapshot", input=payload) as span:
        payload["nested"][0]["content"] = "mutated"
        span.update(output={"result": long_text, "client_secret": "SERVICE_SECRET"})
    row = recorded[0]
    assert row["input"]["nested"][0] == {"access_token": "[REDACTED]", "content": long_text}
    assert json.loads(row["input"]["message"]) == {"authorization": "[REDACTED]", "fact": "keep"}
    assert row["output"] == {"result": long_text, "client_secret": "[REDACTED]"}
    assert payload["nested"][0]["access_token"] == "USER_JWT"


@pytest.mark.asyncio
async def test_traced_step_records_state_delta_and_parent_without_mutating_state(recorded):
    @tracing.traced("graph.step", as_type="chain")
    async def step(state: dict) -> dict:
        state["facts"].append("new")
        return {"answer": "actual answer", "token": "INTERNAL_TOKEN"}

    with tracing.observation("turn", session_id="conversation"):
        result = await step({"facts": ["old"], "access_token": "USER_TOKEN"})
    parent, child = recorded
    assert child["parent_id"] == parent["id"]
    assert child["input"]["state"] == {"facts": ["old"], "access_token": "[REDACTED]"}
    assert child["output"] == {"answer": "actual answer", "token": "[REDACTED]"}
    assert result["token"] == "INTERNAL_TOKEN"


class Answer(BaseModel):
    answer: str


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_llm_attempts_capture_actual_messages_raw_answers_and_usage(
    monkeypatch,
    recorded,
    repaired,
):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    llm = LLMClient("banking-agent")
    await llm.client.close()
    replies = iter(["bad-json", '{"answer":"repaired"}' if repaired else "still-bad"])

    async def complete(**request):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=7),
        )

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    arguments = dict(
        schema=Answer,
        instructions="Original system instruction",
        payload={"fact": "original payload", "api_key": "HIDDEN_KEY"},
        operation="answer",
    )
    if repaired:
        result = await llm.generate_json(**arguments)
        assert result.answer == "repaired"
    else:
        with pytest.raises(LLMResponseError):
            await llm.generate_json(**arguments)
    generations = [row for row in recorded if row["as_type"] == "generation"]
    assert len(generations) == 2
    first, second = generations
    assert "Original system instruction" in first["input"][0]["content"]
    assert json.loads(first["input"][1]["content"]) == {
        "fact": "original payload",
        "api_key": "[REDACTED]",
    }
    assert first["output"] == {"role": "assistant", "content": "bad-json"}
    assert first["level"] == "WARNING"
    assert second["input"][2] == {"role": "assistant", "content": "bad-json"}
    assert "JSON Schema validation" in second["input"][3]["content"]
    assert second["usage_details"] == {"input": 20, "output": 7}
    assert first["model"] == llm.model
    assert first["parent_id"] == second["parent_id"] == recorded[0]["id"]
    if not repaired:
        assert second["output"]["content"] == "still-bad"
        assert second["level"] == "ERROR"


@pytest.mark.asyncio
async def test_mcp_http_and_server_results_join_the_callers_trace(monkeypatch, recorded):
    monkeypatch.setenv("AUTH_DISABLED", "true")

    async def echo(arguments, principal):
        return {"text": arguments["text"], "nested": {"token": "HIDDEN"}}

    app = create_mcp_app("echo", [MCPTool("echo", "Echo", {}, echo)])
    original_client = httpx.AsyncClient
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw)
    )
    client = MCPClient("http://mcp.test/mcp")
    with tracing.observation("turn"):
        result = await client.call("echo", {"text": "full result"}, "USER_JWT")
    server = next(row for row in recorded if row["name"] == "mcp.tool.call")
    http = next(
        row
        for row in recorded
        if row["name"] == "mcp.http" and row["input"]["method"] == "tools/call"
    )
    assert len({row["trace_id"] for row in recorded}) == 1
    assert server["parent_id"] == http["id"]
    assert server["input"]["arguments"] == {"text": "full result"}
    assert server["output"] == {"text": "full result", "nested": {"token": "[REDACTED]"}}
    assert http["output"]["result"]["structuredContent"] == server["output"]
    assert result["nested"]["token"] == "HIDDEN"
    assert "USER_JWT" not in json.dumps(recorded)


def test_mcp_parent_cannot_join_a_different_system(recorded):
    with tracing.observation("banking"):
        parent = get_current_span().get_span_context().trace_id
        headers = tracing.trace_headers()
        headers["x-agent-system-id"] = "travel"
        with tracing.mcp_parent(headers), tracing.observation("other-system"):
            assert get_current_span().get_span_context().trace_id != parent
        assert get_current_span().get_span_context().trace_id == parent


@pytest.mark.asyncio
async def test_memory_read_and_working_write_have_full_api_payloads(monkeypatch, recorded):
    from services.memory_service import app as memory

    monkeypatch.setenv("AUTH_DISABLED", "true")
    memories = [{"id": "memory-1", "content": "Retrieved fact", "memory_type": "episodic"}]
    store = SimpleNamespace(
        ensure_ready=lambda: None,
        search=AsyncMock(return_value=memories),
        memories=SimpleNamespace(
            insert_many=AsyncMock(
                return_value=SimpleNamespace(
                    inserted_ids=["user-message", "assistant-message"],
                )
            )
        ),
        jobs=SimpleNamespace(
            insert_one=AsyncMock(return_value=SimpleNamespace(inserted_id="job-1"))
        ),
    )
    monkeypatch.setattr(memory, "store", store)
    monkeypatch.setattr(memory.embeddings, "embed", AsyncMock(return_value=[0.1, 0.2]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=memory.app), base_url="http://memory.test"
    ) as client:
        recall = await client.post(
            "/api/v1/context",
            json={
                "agent_id": "banking-agent",
                "user_id": "customer-001",
                "query": "Full query",
                "conversation_id": "session",
                "memory_types": ["episodic"],
            },
        )
        assert recall.status_code == 200
        stored = await client.post(
            "/api/v1/working/turns",
            json={
                "agent_id": "banking-agent",
                "user_id": "customer-001",
                "conversation_id": "session",
                "user_message": "Full user message",
                "assistant_message": "Full assistant message",
                "intent": "general",
                "tool_calls": [],
            },
        )
        assert stored.status_code == 202
    read = next(row for row in recorded if row["name"] == "memory.api.enrich_context")
    write = next(row for row in recorded if row["name"] == "memory.api.store_conversation_turn")
    assert read["input"]["payload"]["query"] == "Full query"
    assert read["output"]["memories"] == memories
    assert write["input"]["payload"]["assistant_message"] == "Full assistant message"
    assert write["output"]["migration"]["job_id"] == "job-1"
    assert read["metadata"]["correlation_id"] == write["metadata"]["correlation_id"] == "session"


def test_disabled_or_failed_telemetry_does_not_break_the_operation(monkeypatch, recorded):
    def unavailable():
        raise RuntimeError("collector unavailable")

    monkeypatch.setattr(tracing, "get_client", unavailable)
    with tracing.observation("fail-open") as span:
        span.update(output="completed")
    assert not recorded


def test_real_langfuse_sdk_exports_typed_payloads_and_usage_without_network(monkeypatch):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    monkeypatch.setenv("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-offline-sdk-contract")
    client = Langfuse(
        public_key="pk-offline-sdk-contract",
        secret_key="sk-offline-sdk-contract",
        base_url="http://unused.test",
        tracer_provider=provider,
        span_exporter=exporter,
    )
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    try:
        with tracing.observation("turn", input={"message": "question"}, session_id="session"):
            with tracing.observation(
                "model",
                as_type="generation",
                input=[{"role": "system", "content": "prompt"}],
                model="configured-model",
                model_parameters={"response_format": {"type": "json_object"}},
            ) as span:
                span.update(
                    output={"role": "assistant", "content": "answer"},
                    usage_details={"input": 12, "output": 5},
                )
            with tracing.observation(
                "embedding", as_type="embedding", input=["text"], model="embedding-model"
            ) as span:
                span.update(output={"vectors": [[0.1, 0.2]]}, usage_details={"input": 2})
        client.flush()
        spans = {s.name: s for s in exporter.get_finished_spans()}
        model = spans["model"]
        assert model.parent.span_id == spans["turn"].context.span_id
        assert model.attributes["langfuse.observation.type"] == "generation"
        assert json.loads(model.attributes["langfuse.observation.input"])[0]["content"] == "prompt"
        assert json.loads(model.attributes["langfuse.observation.output"])["content"] == "answer"
        assert json.loads(model.attributes["langfuse.observation.usage_details"]) == {
            "input": 12,
            "output": 5,
        }
        assert spans["embedding"].attributes["langfuse.observation.type"] == "embedding"
    finally:
        client.shutdown()
        provider.shutdown()


@pytest.mark.asyncio
async def test_banking_graph_records_every_executed_node(monkeypatch, recorded):
    from services.banking_agent.graph import BankingGraph

    class Skills:
        async def load_customer_profile(self, token):
            return {"id": "customer-001", "full_name": "Test Customer"}

        async def general(self, state: dict) -> dict:
            return {"response": "draft", "data": {"fact": "skill result"}, "tool_calls": []}

        profile = accounts = operations = investments = travel_plan = general

    async def generate(schema, **kwargs):
        return schema.model_validate(
            {"intent": "general", "rationale": "selected"}
            if kwargs["operation"] == "route_intent"
            else {"message": "final answer"}
        )

    memory = SimpleNamespace(
        recall=AsyncMock(return_value=[{"content": "remembered fact"}]),
        conversation_messages=AsyncMock(return_value=[{"role": "user", "content": "old turn"}]),
        store_turn=AsyncMock(return_value={"turn_id": "stored-turn"}),
    )
    graph = BankingGraph(memory, Skills(), SimpleNamespace(generate_json=generate))
    with tracing.observation("turn", session_id="session"):
        result = await graph.graph.ainvoke(
            {
                "message": "question",
                "user_id": "customer-001",
                "conversation_id": "session",
                "access_token": "USER_JWT",
            }
        )
    steps = [r for r in recorded if r["name"].startswith("banking.step.")]
    assert [r["name"].removeprefix("banking.step.") for r in steps] == [
        "recall_memory",
        "classify",
        "select_abs_identity",
        "general",
        "compose_response",
        "persist_memory",
    ]
    assert all(r["trace_id"] == recorded[0]["trace_id"] for r in steps)
    assert all(r["parent_id"] == recorded[0]["id"] for r in steps)
    assert steps[3]["input"]["state"]["memories"] == [{"content": "remembered fact"}]
    assert steps[3]["output"]["data"]["fact"] == "skill result"
    assert result["response"] == "final answer"
    assert "USER_JWT" not in json.dumps(recorded)
