from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
import pytest

from services.common import auth
from services.memory_service import orchestrator


@pytest.mark.parametrize("value,expected", [(None, 1800), ("0", 0), ("1", 1), ("10", 10)])
def test_memory_interval_supports_short_tests_and_manual_mode(monkeypatch, value, expected):
    monkeypatch.delenv("MEMORY_ORCHESTRATION_INTERVAL_SECONDS", raising=False)
    if value is not None:
        monkeypatch.setenv("MEMORY_ORCHESTRATION_INTERVAL_SECONDS", value)
    assert orchestrator._interval_seconds() == expected


@pytest.mark.parametrize("value", ["-1", "ten", "1.5", ""])
def test_invalid_memory_interval_is_an_explicit_configuration_error(monkeypatch, value):
    monkeypatch.setenv("MEMORY_ORCHESTRATION_INTERVAL_SECONDS", value)
    with pytest.raises(ValueError, match="MEMORY_ORCHESTRATION_INTERVAL_SECONDS"):
        orchestrator._interval_seconds()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [0, 10])
async def test_lifespan_starts_scheduler_only_when_enabled(monkeypatch, interval):
    monkeypatch.setenv("MEMORY_ORCHESTRATION_INTERVAL_SECONDS", str(interval))
    store = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(orchestrator, "store", store)
    monkeypatch.setattr(orchestrator, "_backfill_embeddings", AsyncMock(return_value=0))
    monkeypatch.setattr(orchestrator, "_seed_knowledge", AsyncMock())
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def schedule(seconds):
        assert seconds == interval
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scheduler = AsyncMock(side_effect=schedule)
    monkeypatch.setattr(orchestrator, "_scheduler", scheduler)
    async with orchestrator.lifespan(orchestrator.app):
        if interval:
            await asyncio.wait_for(started.wait(), timeout=1)
        else:
            scheduler.assert_not_called()
        health = await orchestrator.health()
        assert health["schedule_enabled"] is bool(interval)
        assert health["schedule_seconds"] == interval
    store.connect.assert_awaited_once()
    store.close.assert_awaited_once()
    assert cancelled.is_set() is bool(interval)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("provider unavailable")])
async def test_scheduler_waits_the_configured_interval_between_attempts(monkeypatch, failure):
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    collect = AsyncMock(side_effect=failure)
    monkeypatch.setattr(orchestrator.asyncio, "sleep", sleep)
    monkeypatch.setattr(orchestrator, "_collect_jobs", collect)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._scheduler(10)

    assert sleep.await_args_list == [call(10), call(10)]
    collect.assert_awaited_once_with(orchestrator.CollectRequest(limit=500), trigger="schedule")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client,audience,expected_status",
    [
        (None, None, 401),
        ("bank-ui", "memory-orchestrator", 403),
        ("banking-agent", "memory-service", 403),
        ("memory-operator", "memory-service", 403),
        ("memory-operator", "memory-orchestrator", 200),
    ],
)
async def test_manual_collection_in_manual_mode_requires_operator_token(
    monkeypatch, client, audience, expected_status
):
    monkeypatch.setenv("MEMORY_ORCHESTRATION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("ALLOWED_AZP", "memory-operator")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "memory-orchestrator")
    monkeypatch.setattr(
        auth._verifier, "verify",
        AsyncMock(return_value={"sub": "operator", "azp": client, "aud": audience}),
    )
    result = {"completed_jobs": 1, "failed_attempts": 0, "created": []}
    collect = AsyncMock(return_value=result)
    monkeypatch.setattr(orchestrator, "_collect_jobs", collect)
    request = {"agent_id": "banking-agent", "conversation_id": "poison-test", "limit": 5}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=orchestrator.app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/api/v1/orchestrator/collect", json=request,
            headers={"Authorization": "Bearer test-token"} if client else {},
        )

    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json() == result
        collect.assert_awaited_once_with(
            orchestrator.CollectRequest(**request), trigger="api:memory-operator"
        )
    else:
        collect.assert_not_awaited()
