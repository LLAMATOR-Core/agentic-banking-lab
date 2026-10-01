from __future__ import annotations

from typing import Any

import pytest

from services.banking_agent.graph import BankingGraph


class FakeLLM:
    def __init__(self, intent: str) -> None:
        self.intent = intent
        self.calls = 0

    async def generate_json(self, schema, **_: Any):
        self.calls += 1
        return schema(intent=self.intent, rationale="test decision")


def graph_with(fake: FakeLLM) -> BankingGraph:
    graph = object.__new__(BankingGraph)
    graph.llm = fake
    return graph


@pytest.mark.asyncio
async def test_llm_routes_core_skill() -> None:
    fake = FakeLLM("accounts")
    result = await graph_with(fake).classify({"message": "Какой остаток?", "memories": []})
    assert result["intent"] == "accounts"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_structured_action_overrides_llm_but_model_is_required() -> None:
    fake = FakeLLM("general")
    result = await graph_with(fake).classify(
        {
            "message": "просто привет",
            "action": {"type": "transfer", "parameters": {}},
            "memories": [],
        }
    )
    assert result["intent"] == "action"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_structured_travel_request_overrides_wrong_llm_route() -> None:
    fake = FakeLLM("general")
    result = await graph_with(fake).classify(
        {"message": "поездка", "travel": {"destination": "Стамбул"}, "memories": []}
    )
    assert result["intent"] == "travel"
