from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any

from services.common import tracing


class FakeSpan:
    def update(self, **_: Any) -> None:
        return None


class FakeObservationManager:
    def __enter__(self) -> FakeSpan:
        return FakeSpan()

    def __exit__(self, *_: Any) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start_as_current_observation(self, **kwargs: Any) -> FakeObservationManager:
        self.started.append(kwargs)
        return FakeObservationManager()


def test_trace_identity_is_propagated_to_langfuse_v4(monkeypatch) -> None:
    client = FakeClient()
    propagated: list[dict[str, Any]] = []

    @contextmanager
    def fake_propagate_attributes(**kwargs: Any):
        propagated.append(kwargs)
        yield

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    monkeypatch.setattr(tracing, "propagate_attributes", fake_propagate_attributes)

    with tracing.observation(
        "banking-agent.turn",
        metadata={"component": "banking"},
        user_id="customer-1",
        session_id="conversation-1",
        agent_id="banking-agent",
    ):
        pass

    assert client.started[0]["metadata"] == {
        "component": "banking",
        "agent_id": "banking-agent",
        "user_id": "customer-1",
        "correlation_id": "conversation-1",
    }
    assert propagated == [
        {
            "user_id": "customer-1",
            "session_id": "conversation-1",
            "metadata": {
                "component": "banking",
                "agent_id": "banking-agent",
                "user_id": "customer-1",
                "correlation_id": "conversation-1",
            },
            "tags": ["agent:banking-agent"],
            "trace_name": "banking-agent.turn",
        }
    ]


def test_system_boundary_starts_a_new_root_and_restores_caller_context(monkeypatch):
    from opentelemetry.context import attach, detach
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        get_current_span,
        set_span_in_context,
    )

    parent = NonRecordingSpan(SpanContext(123, 456, False, TraceFlags(1)))
    seen_parents = []

    class IsolatedClient(FakeClient):
        def start_as_current_observation(self, **kwargs):
            seen_parents.append(get_current_span().get_span_context().trace_id)
            return super().start_as_current_observation(**kwargs)

    client = IsolatedClient()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-travel")
    monkeypatch.setenv("AGENT_SYSTEM_ID", "travel")
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    monkeypatch.setattr(tracing, "propagate_attributes", lambda **kwargs: nullcontext())
    token = attach(set_span_in_context(parent))
    try:
        with tracing.observation(
            "a2a.travel.send-message",
            system_boundary=True,
            agent_id="travel-agent",
            session_id="shared-correlation",
        ):
            pass
        assert get_current_span().get_span_context().trace_id == 123
    finally:
        detach(token)
    assert seen_parents == [0]
    assert client.started[0]["metadata"]["system_id"] == "travel"
    assert client.started[0]["metadata"]["correlation_id"] == "shared-correlation"
