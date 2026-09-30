from __future__ import annotations

import re
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from services.banking_agent.clients import MemoryClient
from services.banking_agent.skills import BankingSkills


class BankingState(TypedDict, total=False):
    message: str
    user_id: str
    conversation_id: str
    access_token: str
    action: dict[str, Any] | None
    confirm_action_id: str | None
    travel: dict[str, Any] | None
    memories: list[dict[str, Any]]
    intent: str
    response: str
    data: dict[str, Any]
    tool_calls: list[str]
    requires_confirmation: bool
    confirmation_id: str | None


Intent = Literal["profile", "accounts", "operations", "investments", "travel", "action", "general"]


def classify_intent(
    message: str, *, has_action: bool = False, has_confirmation: bool = False
) -> Intent:
    if has_action or has_confirmation:
        return "action"
    normalized = re.sub(r"[^\wа-яё]+", " ", message.lower())
    rules: list[tuple[Intent, tuple[str, ...]]] = [
        ("travel", ("поезд", "путеше", "отел", "рейс", "билет", "travel", "trip")),
        ("investments", ("инвест", "портфел", "облигац", "акци", "рынок", "новост")),
        ("operations", ("операц", "истори", "транзакц", "покупк", "расход")),
        ("accounts", ("счет", "счёт", "баланс", "остаток", "account")),
        ("profile", ("кто я", "профил", "данные клиент", "vip")),
        ("action", ("перев", "оплат", "купить фонд", "купить облигац")),
    ]
    for intent, needles in rules:
        if any(needle in normalized for needle in needles):
            return intent
    return "general"


class BankingGraph:
    def __init__(self, memory: MemoryClient, skills: BankingSkills) -> None:
        self.memory = memory
        self.skills = skills
        builder = StateGraph(BankingState)
        builder.add_node("recall_memory", self.recall_memory)
        builder.add_node("classify", self.classify)
        builder.add_node("profile", skills.profile)
        builder.add_node("accounts", skills.accounts)
        builder.add_node("operations", skills.operations)
        builder.add_node("investments", skills.investments)
        builder.add_node("travel", skills.travel_plan)
        builder.add_node("action", self.action)
        builder.add_node("general", skills.general)
        builder.add_node("persist_memory", self.persist_memory)
        builder.add_edge(START, "recall_memory")
        builder.add_edge("recall_memory", "classify")
        builder.add_conditional_edges("classify", self.route)
        for name in (
            "profile",
            "accounts",
            "operations",
            "investments",
            "travel",
            "action",
            "general",
        ):
            builder.add_edge(name, "persist_memory")
        builder.add_edge("persist_memory", END)
        self.graph = builder.compile()

    async def recall_memory(self, state: BankingState) -> BankingState:
        memories = await self.memory.recall(
            state["user_id"], state["message"], state["conversation_id"]
        )
        return {"memories": memories}

    async def classify(self, state: BankingState) -> BankingState:
        intent = classify_intent(
            state["message"],
            has_action=bool(state.get("action")),
            has_confirmation=bool(state.get("confirm_action_id")),
        )
        return {"intent": intent}

    def route(self, state: BankingState) -> Intent:
        return state["intent"]  # type: ignore[return-value]

    async def action(self, state: BankingState) -> BankingState:
        if state.get("confirm_action_id"):
            return await self.skills.confirm_action(state)
        return await self.skills.prepare_action(state)

    async def persist_memory(self, state: BankingState) -> BankingState:
        # Deliberately store intent and a bounded user utterance, not balances/tool payloads.
        await self.memory.create(
            user_id=state["user_id"],
            memory_type="working",
            content=f"Intent={state['intent']}; user_message={state['message'][:500]}",
            conversation_id=state["conversation_id"],
            metadata={"intent": state["intent"], "tool_calls": state.get("tool_calls", [])},
        )
        if state["intent"] == "travel" and state.get("data", {}).get("request"):
            request = state["data"]["request"]
            await self.memory.create(
                user_id=state["user_id"],
                memory_type="episodic",
                content=(
                    f"Пользователь планировал поездку в {request['destination']}; "
                    f"интересы: {', '.join(request.get('interests', []))}."
                ),
                conversation_id=None,
                metadata={
                    "destination": request["destination"],
                    "interests": request.get("interests", []),
                },
            )
        return {}
