from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from services.banking_agent import skills
from services.banking_agent.skills import BankingSkills


class FakeMCP:
    def __init__(self, operations: list[dict[str, Any]]) -> None:
        self.operations = operations
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(self, name: str, arguments: dict[str, Any], token: str):
        self.calls.append((name, arguments, token))
        return self.operations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 11, 15, 12, tzinfo=UTC),
        datetime(2026, 11, 15, 22, tzinfo=UTC),
        datetime(2026, 10, 31, 21, 10, tzinfo=UTC),
    ],
)
async def test_operations_calculates_current_month_outgoing_totals(monkeypatch, now) -> None:
    timezone = ZoneInfo("Europe/Moscow")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(skills, "datetime", FrozenDateTime)
    monkeypatch.setattr(skills, "BANK_TIMEZONE", timezone)
    month_start = (
        now.astimezone(timezone)
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    previous_month = month_start - timedelta(microseconds=1)
    operations = [
        {
            "direction": "outgoing",
            "amount": "120.50",
            "currency": "RUB",
            "counterparty": "Кафе",
            "status": "completed",
            "created_at": month_start.isoformat(),
        },
        {
            "direction": "incoming",
            "amount": "5000.00",
            "currency": "RUB",
            "counterparty": "Работодатель",
            "status": "completed",
            "created_at": now.isoformat(),
        },
        {
            "direction": "outgoing",
            "amount": "79.50",
            "currency": "RUB",
            "counterparty": "Такси",
            "status": "completed",
            "created_at": now.isoformat(),
        },
        {
            "direction": "outgoing",
            "amount": "999.00",
            "currency": "RUB",
            "counterparty": "Старый платёж",
            "status": "completed",
            "created_at": previous_month.isoformat(),
        },
    ]
    skill = object.__new__(BankingSkills)
    skill.mcp = FakeMCP(operations)

    result = await skill.operations({"access_token": "token"})

    assert result["data"]["monthly_spend"] == {"RUB": "200.00"}
    assert "200.00 RUB" in result["response"]
    assert skill.mcp.calls == [("get_operations_history", {"limit": 100}, "token")]
