from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.banking_agent.skills import BankingSkills


class FakeMCP:
    def __init__(self, operations: list[dict[str, Any]]) -> None:
        self.operations = operations
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(self, name: str, arguments: dict[str, Any], token: str):
        self.calls.append((name, arguments, token))
        return self.operations


@pytest.mark.asyncio
async def test_operations_calculates_current_month_outgoing_totals() -> None:
    now = datetime.now(UTC)
    previous_month = now.replace(day=1) - timedelta(days=1)
    operations = [
        {
            "direction": "outgoing",
            "amount": "120.50",
            "currency": "RUB",
            "counterparty": "Кафе",
            "status": "completed",
            "created_at": now.isoformat(),
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
