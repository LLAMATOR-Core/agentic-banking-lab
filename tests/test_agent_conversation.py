"""Conversational travel clarification in the active, local-agent path."""

from unittest.mock import AsyncMock

import pytest

from services.banking_agent.agents import Answer, SkillAgent, Trip, travel_question
from services.common.skill_contract import Experiment, SkillTask


def travel_agent(monkeypatch, trips):
    agent = SkillAgent("travel")
    history, generations = [], []
    travel_state = None

    async def memory(operation, payload, token):
        nonlocal travel_state
        if operation == "context":
            return {"memory_mode": "shared", "memories": []}
        if operation == "history":
            return {"messages": list(history), "travel_state": travel_state}
        if operation == "turns":
            travel_state = payload.get("travel_state")
            history.extend(
                [
                    {"role": "user", "content": payload["query"]},
                    {"role": "assistant", "content": payload["assistant_message"]},
                ]
            )
        return {"job_id": "test-job"}

    async def generate(schema, **kwargs):
        generations.append(kwargs)
        if schema is Trip:
            return next(trips)
        assert schema is Answer
        return Answer(message="Давайте распределим бюджет по основным статьям расходов.")

    monkeypatch.setattr(agent.memory, "request", memory)
    monkeypatch.setattr(agent.llm, "generate_json", generate)
    monkeypatch.setattr(agent.partner, "plan", AsyncMock(return_value={"agent_summary": "Тест"}))
    return agent, history, generations


@pytest.mark.asyncio
async def test_budget_clarification_then_short_answer_preserves_history_and_partner_contract(
    monkeypatch,
):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "travel-agent")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    from services.travel_agent.app import TripRequest

    agent, history, generations = travel_agent(
        monkeypatch,
        iter(
            [
                Trip(request_type="budget_plan", destination="Таиланд"),
                Trip(
                    request_type="budget_plan",
                    destination="Таиланд",
                    origin="Москва",
                    travel_period="январь",
                    duration_days=10,
                    adults=2,
                    interests=[f"interest-{i}" for i in range(10)],
                ),
            ]
        ),
    )
    first = await agent.run(
        SkillTask(
            message="Я хочу поехать в тайланд, давай спланируем мои траты",
            conversation_id="budget-conversation",
            experiment=Experiment(run_id="test-budget"),
        ),
        user_id="customer-001",
        subject_token="test-token",
    )
    assert first["response"] == (
        "Давайте прикинем бюджет поездки. Из какого города будете вылетать? "
        "Когда примерно и на сколько дней планируете поездку?"
    )
    assert set(first["data"]["missing_fields"]) == {
        "origin",
        "travel_period",
        "duration_days",
        "adults",
    }
    agent.partner.plan.assert_not_awaited()
    assert history[-1]["content"] == first["response"]

    await agent.run(
        SkillTask(
            message="Из Москвы, в январе на десять дней, вдвоём",
            conversation_id="budget-conversation",
            experiment=Experiment(run_id="test-budget"),
        ),
        user_id="customer-001",
        subject_token="test-token",
    )
    extraction = generations[1]
    assert extraction["operation"] == "extract_trip"
    assert extraction["payload"]["conversation_history"][-1]["content"] == first["response"]
    request = agent.partner.plan.call_args.args[0]
    validated = TripRequest.model_validate(request)
    assert validated.request_type == "budget_plan"
    assert validated.destination == "Таиланд" and validated.adults == 2
    assert validated.origin is None and validated.departure_date is None
    answer_trip = generations[-1]["payload"]["trip"]
    assert answer_trip["origin"] == "Москва"
    assert answer_trip["travel_period"] == "январь"
    assert answer_trip["duration_days"] == 10 and answer_trip["adults"] == 2
    assert validated.duration_days == 10 and validated.travel_period == "январь"


@pytest.mark.asyncio
async def test_phuket_budget_preserves_outbound_route_and_separates_stay_end(monkeypatch):
    agent, _, generations = travel_agent(
        monkeypatch,
        iter(
            [
                Trip(
                    request_type="budget_plan",
                    destination="Пхукет",
                    origin="LED",
                    destination_airport="HKT",
                    departure_date="2026-12-03",
                    stay_end_date="2026-12-20",
                    one_way=True,
                    adults=3,
                ),
            ]
        ),
    )
    await agent.run(
        SkillTask(
            message="3 взрослых на Пхукет 3–20 декабря, далее Шанхай, билет туда не учитывай",
            conversation_id="phuket-budget",
            experiment=Experiment(run_id="phuket-test"),
        ),
        user_id="customer-001",
        subject_token="test-token",
    )
    request = agent.partner.plan.call_args.args[0]
    assert request["request_type"] == "budget_plan"
    assert request["origin"] == "LED" and request["destination_airport"] == "HKT"
    assert request["departure_date"] == "2026-12-03"
    assert request["stay_end_date"] == "2026-12-20" and request["one_way"] is True
    assert "return_date" not in request and request["adults"] == 3
    assert generations[-1]["payload"]["trip"]["stay_end_date"] == "2026-12-20"


@pytest.mark.parametrize("bad_question", [None, "", "Уточните: origin, departure_date"])
@pytest.mark.asyncio
async def test_flight_clarification_asks_dates_without_repeating_known_route(
    monkeypatch, bad_question
):
    agent, _, _ = travel_agent(
        monkeypatch,
        iter(
            [
                Trip(
                    request_type="trip_search",
                    destination="Бангкок",
                    origin="MOW",
                    destination_airport="BKK",
                    clarification_question=bad_question,
                )
            ]
        ),
    )
    result = await agent.run(
        SkillTask(
            message="Найди билеты из Москвы в Бангкок",
            conversation_id="flight",
            experiment=Experiment(run_id="test-flight"),
        ),
        user_id="customer-001",
        subject_token="test-token",
    )
    assert result["response"] == "Помогу спланировать поездку. На какие даты планируете поездку?"
    agent.partner.plan.assert_not_awaited()


def test_model_can_use_a_natural_clarification():
    trip = Trip(
        request_type="budget_plan",
        destination="Таиланд",
        clarification_question="Давайте прикинем расходы на Таиланд. Когда хотите поехать?",
    )
    assert travel_question(trip, ["travel_period"]) == trip.clarification_question


@pytest.mark.asyncio
async def test_short_year_update_cannot_erase_saved_route(monkeypatch):
    agent, _, generations = travel_agent(
        monkeypatch,
        iter(
            [
                Trip(
                    request_type="budget_plan",
                    origin="LED",
                    destination="Пхукет",
                    destination_airport="HKT",
                    departure_date="2026-12-03",
                    stay_end_date="2026-12-20",
                    one_way=True,
                    adults=3,
                ),
                Trip(request_type="budget_plan", departure_date="2026-12-03"),
            ]
        ),
    )
    for message in ["Пхукет с 3 по 20 декабря, трое, дальше Шанхай не учитывай", "2026 год"]:
        await agent.run(
            SkillTask(
                message=message,
                conversation_id="trip-state",
                experiment=Experiment(run_id="trip-state"),
            ),
            user_id="customer-001",
            subject_token="test-token",
        )
    assert agent.partner.plan.await_count == 2
    first, second = [c.args[0] for c in agent.partner.plan.call_args_list]
    for field in [
        "origin",
        "destination_airport",
        "departure_date",
        "stay_end_date",
        "adults",
        "one_way",
    ]:
        assert second[field] == first[field]
    assert "return_date" not in second
    assert generations[2]["payload"]["previous_trip"]["origin"] == "LED"
