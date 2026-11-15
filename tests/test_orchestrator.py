"""The orchestrator owns the answer and can consult other agents without forwarding their data."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from services.banking_agent.orchestrator import OrchestratorGraph, ResponsePlan, Routing
from services.common.llm import LLMResponseError
from services.common.skill_contract import SKILLS


def make_graph(intent, decisions, source_data=None):
    history = [{"role": "user", "content": "Сохранённый запрос"}]
    memory = SimpleNamespace(
        last_skill=AsyncMock(return_value="travel"),
        conversation_messages=AsyncMock(return_value=history),
        store_turn=AsyncMock(),
    )
    generations, calls = [], []
    decisions = iter(decisions)

    async def generate(schema, **kwargs):
        generations.append(deepcopy(kwargs))
        return schema(intent=intent) if schema is Routing else ResponsePlan(**next(decisions))

    agents = {}
    for kind, (identity, _) in SKILLS.items():

        async def run(task, *, user_id, subject_token, kind=kind):
            calls.append((kind, task.model_dump(), user_id, subject_token))
            return {
                "response": f"Черновик {kind}",
                "data": {
                    **(source_data or {}).get(kind, {kind: {"fact": kind}}),
                    "experiment": {"run_id": task.experiment.run_id, "staged": []},
                },
                "tool_calls": [f"skill.{kind}", "memory.recall"],
            }

        agents[kind] = SimpleNamespace(identity=identity, run=run)
    graph = OrchestratorGraph(SimpleNamespace(generate_json=generate), memory, agents)
    state = {
        "message": "Сопоставь поездку с моими остатками и расходами",
        "user_id": "customer-001",
        "conversation_id": "session-test",
        "access_token": "ORIGINAL_JWT_CANARY",
        "experiment": {"run_id": "run-test"},
    }
    return graph, state, memory, generations, calls


@pytest.mark.asyncio
async def test_orchestrator_consults_another_agent_and_stores_its_own_answer():
    graph, state, memory, generations, calls = make_graph(
        "travel",
        [{"next_agent": "operations"}, {"response": "Итоговый план и расходы"}],
        {
            "travel": {"travel": {"recommendations": [{"url": "https://example.test/travel"}]}},
            "operations": {"accounts": [{"balance": "1000.00"}], "private": "BANK_CANARY"},
        },
    )
    state["travel"] = {"request_type": "destination_info", "destination": "Стамбул"}
    result = await graph.ainvoke(state)

    assert [row[0] for row in calls] == ["travel", "operations"]
    assert result["response"] == "Итоговый план и расходы"
    assert result["intent"] == "travel"
    assert result["data"]["orchestration"]["delegated_agents"] == ["travel", "operations"]
    assert result["data"]["travel"]["recommendations"]
    assert result["data"]["agent_results"]["operations"]["data"]["accounts"]
    assert result["data"]["experiment"]["run_id"] == "run-test"
    assert result["tool_calls"].count("memory.recall") == 1
    assert "llm.orchestrator_response" in result["tool_calls"]
    memory.store_turn.assert_awaited_once()
    assert memory.store_turn.call_args.kwargs["assistant_message"] == result["response"]
    assert generations[0]["payload"] == {
        "message": state["message"],
        "has_travel": True,
        "previous_skill": "travel",
    }
    assert len(generations[1]["payload"]["agent_results"]) == 1
    assert len(generations[2]["payload"]["agent_results"]) == 2
    assert (
        generations[-1]["payload"]["conversation_history"]
        == memory.conversation_messages.return_value
    )
    assert "BANK_CANARY" in str(generations[-1]["payload"])
    assert "ORIGINAL_JWT_CANARY" not in str(generations)
    # Agents receive the user's task and their own history, not the other agent's results.
    for kind, task, user_id, token in calls:
        assert task["message"] == state["message"]
        assert "BANK_CANARY" not in str(task)
        assert user_id == state["user_id"] and token == state["access_token"]
        assert task["travel"] == (state["travel"] if kind == "travel" else None)


@pytest.mark.asyncio
async def test_general_response_is_composed_without_calling_agents():
    graph, state, memory, generations, calls = make_graph(
        "general", [{"response": "Здравствуйте!"}]
    )
    result = await graph.ainvoke(state)
    assert result["response"] == "Здравствуйте!" and result["intent"] == "general"
    assert not calls
    assert result["data"]["orchestration"]["delegated_agents"] == []
    assert generations[-1]["operation"] == "orchestrator_response"
    memory.store_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_can_recover_a_general_route_and_choose_an_agent():
    graph, state, _, _, calls = make_graph(
        "general",
        [{"next_agent": "investments"}, {"response": "Оценка портфеля"}],
    )
    result = await graph.ainvoke(state)
    assert [row[0] for row in calls] == ["investments"]
    assert result["intent"] == "investments"
    assert "investments" in result["data"]


@pytest.mark.asyncio
async def test_repeated_delegation_is_rejected_without_a_second_agent_call():
    graph, state, memory, _, calls = make_graph("travel", [{"next_agent": "travel"}])
    with pytest.raises(LLMResponseError, match="уже вызванного"):
        await graph.ainvoke(state)
    assert [row[0] for row in calls] == ["travel"]
    memory.store_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_agents_are_bounded_and_conflicting_sources_remain_separate():
    graph, state, _, generations, calls = make_graph(
        "operations",
        [{"next_agent": "investments"}, {"next_agent": "travel"}, {"response": "Итог"}],
        {
            "operations": {"portfolio": "ABS_SNAPSHOT_A"},
            "investments": {"portfolio": "ABS_SNAPSHOT_B"},
        },
    )
    result = await graph.ainvoke(state)
    assert len(calls) == len(SKILLS) == 3
    assert generations[-1]["payload"]["available_agents"] == []
    assert result["data"]["portfolio"] == "ABS_SNAPSHOT_A"
    assert result["data"]["agent_results"]["investments"]["data"]["portfolio"] == "ABS_SNAPSHOT_B"


@pytest.mark.asyncio
async def test_structured_trip_still_selects_travel_before_final_composition():
    graph, state, _, _, calls = make_graph("operations", [{"response": "Маршрут"}])
    state["travel"] = {"request_type": "destination_info", "destination": "Стамбул"}
    result = await graph.ainvoke(state)
    assert result["intent"] == "travel" and [row[0] for row in calls] == ["travel"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"response": "Ответ", "next_agent": "travel"},
        {"response": " "},
        {"next_agent": "transfer"},
    ],
)
def test_response_plan_requires_a_final_answer_or_one_known_agent(payload):
    with pytest.raises(ValidationError):
        ResponsePlan.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["action", "confirm_action_id"])
async def test_money_actions_are_rejected_before_orchestration(field):
    graph, state, memory, generations, calls = make_graph("operations", [])
    state[field] = {"type": "transfer"} if field == "action" else "confirmation-001"
    with pytest.raises(ValueError, match="недоступны"):
        await graph.ainvoke(state)
    assert not calls and not generations
    memory.last_skill.assert_not_awaited()
