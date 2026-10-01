from __future__ import annotations

from typing import Any

import pytest

from services.banking_agent.graph import BankingGraph


class FakeMemory:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    async def store_turn(self, **kwargs: Any) -> None:
        self.turns.append(kwargs)


@pytest.mark.asyncio
async def test_complete_turn_is_saved_to_working_memory() -> None:
    graph = object.__new__(BankingGraph)
    graph.memory = FakeMemory()
    state = {
        "user_id": "customer-001",
        "conversation_id": "conversation-1",
        "message": "Сколько я потратил?",
        "response": "За месяц вы потратили 200.00 RUB.",
        "intent": "operations",
        "tool_calls": ["abs.get_operations_history", "llm.compose_response"],
    }

    result = await graph.persist_memory(state)

    assert result == {}
    assert graph.memory.turns == [
        {
            "user_id": "customer-001",
            "conversation_id": "conversation-1",
            "user_message": "Сколько я потратил?",
            "assistant_message": "За месяц вы потратили 200.00 RUB.",
            "intent": "operations",
            "tool_calls": ["abs.get_operations_history", "llm.compose_response"],
        }
    ]
