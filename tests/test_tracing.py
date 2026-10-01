from __future__ import annotations

from contextlib import contextmanager
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
    }
    assert propagated == [
        {
            "user_id": "customer-1",
            "session_id": "conversation-1",
            "metadata": {
                "component": "banking",
                "agent_id": "banking-agent",
                "user_id": "customer-1",
            },
            "tags": ["agent:banking-agent"],
            "trace_name": "banking-agent.turn",
        }
    ]
