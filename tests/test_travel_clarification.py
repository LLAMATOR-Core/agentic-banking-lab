from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.banking_agent.graph import BankingGraph
from services.banking_agent.skills import BankingSkills


def make_skills(extracted):
    skills = object.__new__(BankingSkills)

    async def extract(schema, **kwargs):
        return schema(**extracted)

    skills.llm = SimpleNamespace(generate_json=AsyncMock(side_effect=extract))
    skills.travel = SimpleNamespace(plan=AsyncMock())
    return skills


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [None, "", " \n\t"])
async def test_empty_model_question_asks_for_missing_dates_without_calling_travel(question):
    skills = make_skills(
        {
            "request_type": "trip_search",
            "origin": "MOW",
            "destination": "Ереван",
            "destination_airport": "EVN",
            "clarification_question": question,
        }
    )

    result = await skills.travel_plan(
        {"message": "Найди билеты из Москвы в Ереван", "user_id": "customer-001"}
    )

    assert result["response"] == "Можете уточнить дату вылета и дату возвращения?"
    assert result["data"]["missing_fields"] == ["departure_date", "return_date"]
    skills.travel.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonempty_model_question_is_preserved():
    skills = make_skills(
        {
            "request_type": "destination_info",
            "clarification_question": "  О каком городе рассказать? ",
        }
    )

    result = await skills.travel_plan({"message": "Что посмотреть?", "user_id": "customer-001"})

    assert result["response"] == "О каком городе рассказать?"
    assert result["data"]["missing_fields"] == ["destination"]
    skills.travel.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_destination_recommendations_only_require_destination():
    skills = make_skills({"request_type": "destination_info"})

    result = await skills.travel_plan({"message": "Что посмотреть?", "user_id": "customer-001"})

    assert result["response"] == "Можете уточнить город назначения?"
    assert result["data"]["missing_fields"] == ["destination"]
    skills.travel.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_structured_request_does_not_require_model_question():
    skills = make_skills({})

    result = await skills.travel_plan(
        {"message": "Найди поездку", "travel": {"origin": "MOW"}, "user_id": "customer-001"}
    )

    assert result["response"] == (
        "Можете уточнить город назначения, дату вылета и дату возвращения?"
    )
    assert result["data"]["missing_fields"] == [
        "destination", "destination_airport", "departure_date", "return_date"
    ]
    skills.llm.generate_json.assert_not_awaited()
    skills.travel.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_clarification_is_saved_and_next_turn_can_complete_the_search():
    departure = (date.today() + timedelta(days=30)).isoformat()
    return_date = (date.today() + timedelta(days=37)).isoformat()
    extracted = {
        "request_type": "trip_search", "origin": "MOW",
        "destination": "Ереван", "destination_airport": "EVN",
    }
    history = []
    calls = []

    async def store_turn(**turn):
        history.extend([
            {"role": "user", "content": turn["user_message"]},
            {"role": "assistant", "content": turn["assistant_message"]},
        ])

    async def generate(schema, *, operation, payload, **kwargs):
        calls.append(operation)
        if operation == "route_intent":
            return schema(intent="travel", rationale="Продолжение поиска поездки")
        if operation == "extract_travel_request":
            if history:
                assert payload["conversation_history"] == history
                return schema(**extracted, departure_date=departure, return_date=return_date)
            return schema(**extracted)
        assert operation == "compose_response"
        assert len(history) == 2, "A clarification must bypass the answer composer"
        return schema(message="Варианты поездки найдены.")

    memory = SimpleNamespace(
        recall=AsyncMock(return_value=[]),
        conversation_messages=AsyncMock(side_effect=lambda *args: list(history)),
        store_turn=AsyncMock(side_effect=store_turn),
    )
    skills = make_skills(extracted)
    skills.llm = SimpleNamespace(generate_json=AsyncMock(side_effect=generate))
    skills.load_customer_profile = AsyncMock(return_value={"id": "customer-001"})
    skills.travel.plan.return_value = {"flights": [], "accommodations": []}
    graph = BankingGraph(memory, skills, skills.llm)
    common = {
        "user_id": "customer-001", "conversation_id": "trip-clarification", "access_token": "test"
    }

    first = await graph.graph.ainvoke({**common, "message": "Найди билеты из Москвы в Ереван"})

    assert first["response"] == "Можете уточнить дату вылета и дату возвращения?"
    assert history[-1]["content"] == first["response"]
    assert calls == ["route_intent", "extract_travel_request"]
    skills.travel.plan.assert_not_awaited()

    second = await graph.graph.ainvoke({**common, "message": f"С {departure} по {return_date}"})

    assert second["response"] == "Варианты поездки найдены."
    request, conversation = skills.travel.plan.await_args.args
    assert request["departure_date"] == departure
    assert request["return_date"] == return_date
    assert request["origin"] == "MOW"
    assert request["destination_airport"] == "EVN"
    assert "clarification_question" not in request
    assert "missing_fields" not in request
    assert conversation == common["conversation_id"]
    assert len(history) == 4
