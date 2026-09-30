from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from services.banking_agent.clients import MemoryClient, TravelA2AClient
from services.common.auth import ServiceTokenProvider
from services.common.mcp import MCPClient


class BankingSkills:
    def __init__(
        self,
        memory: MemoryClient,
        travel: TravelA2AClient,
        tokens: ServiceTokenProvider | None = None,
    ) -> None:
        self.mcp = MCPClient(os.getenv("ABS_MCP_URL", "http://abs-mcp:8101/mcp"))
        self.duckduckgo = MCPClient(
            os.getenv("DUCKDUCKGO_MCP_URL", "http://duckduckgo-mcp:8105/mcp"), timeout=18
        )
        self.memory = memory
        self.travel = travel
        self.tokens = tokens

    async def profile(self, state: dict[str, Any]) -> dict[str, Any]:
        profile = await self.mcp.call("get_customer_info", {}, state["access_token"])
        return {
            "response": (
                f"Клиент: {profile['full_name']}. Тип: {profile['client_type']}; "
                f"VIP: {'да' if profile['vip'] else 'нет'}."
            ),
            "data": {"customer": profile},
            "tool_calls": ["abs.get_customer_info"],
        }

    async def accounts(self, state: dict[str, Any]) -> dict[str, Any]:
        accounts = await self.mcp.call("get_customer_accounts", {}, state["access_token"])
        lines = [
            f"{item['kind']} {item['masked_number']}: {item['balance']} {item['currency']}"
            for item in accounts
        ]
        return {
            "response": "Ваши счета:\n" + "\n".join(f"• {line}" for line in lines),
            "data": {"accounts": accounts},
            "tool_calls": ["abs.get_customer_accounts"],
        }

    async def operations(self, state: dict[str, Any]) -> dict[str, Any]:
        operations = await self.mcp.call(
            "get_operations_history", {"limit": 10}, state["access_token"]
        )
        if not operations:
            text = "Операций пока нет."
        else:
            rows = [
                f"{item['direction']}: {item['amount']} {item['currency']} — {item['counterparty']}"
                for item in operations[:5]
            ]
            text = "Последние операции:\n" + "\n".join(f"• {row}" for row in rows)
        return {
            "response": text,
            "data": {"operations": operations},
            "tool_calls": ["abs.get_operations_history"],
        }

    async def investments(self, state: dict[str, Any]) -> dict[str, Any]:
        products, portfolio, news = await asyncio.gather(
            self.mcp.call("list_investment_products", {}, state["access_token"]),
            self.mcp.call("get_portfolio", {}, state["access_token"]),
            self._investment_news(state["message"]),
        )
        total = sum(Decimal(item["market_value"]) for item in portfolio)
        news_text = "Доступ к DuckDuckGo временно недоступен."
        if news:
            news_text = " Свежие материалы: " + "; ".join(item["title"] for item in news[:3])
        return {
            "response": (
                f"Стоимость тестового портфеля: {total:.2f} RUB. "
                f"Доступно продуктов: {len(products)}.{news_text} "
                "Это не индивидуальная инвестиционная рекомендация."
            ),
            "data": {"products": products, "portfolio": portfolio, "news": news},
            "tool_calls": [
                "abs.list_investment_products",
                "abs.get_portfolio",
                "duckduckgo.news_search",
            ],
        }

    async def travel_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        request = self._trip_request(state)
        result = await self.travel.plan(request, state["conversation_id"])
        cheapest = result["packages"][0] if result.get("packages") else None
        if cheapest:
            summary = (
                f"Нашёл вариант в {request['destination']}: перелёт + отель от "
                f"{cheapest['total']} {cheapest['currency']}."
            )
        else:
            summary = "Партнёрский агент не нашёл пакет по заданным параметрам."
        return {
            "response": f"{summary} Бронирование не выполнялось; проверьте цену у поставщика.",
            "data": {"travel": result, "request": request},
            "tool_calls": ["a2a.travel.plan-trip", *result.get("tool_calls", [])],
        }

    async def prepare_action(self, state: dict[str, Any]) -> dict[str, Any]:
        action = state.get("action")
        if not action:
            return {
                "response": "Для операции заполните защищённую форму в чате.",
                "data": {},
                "tool_calls": [],
            }
        action_type = action["type"]
        parameters = self._validate_action(action_type, action["parameters"])
        expires_at = datetime.now(UTC) + timedelta(minutes=15)
        record = await self.memory.create(
            user_id=state["user_id"],
            memory_type="working",
            content=f"Pending financial action: {action_type}",
            conversation_id=state["conversation_id"],
            metadata={"status": "pending", "action_type": action_type, "parameters": parameters},
            expires_at=expires_at,
        )
        description = self._action_description(action_type, parameters)
        return {
            "response": (
                f"Подготовлена операция: {description}. "
                "Проверьте параметры и подтвердите отдельно. "
                "Подтверждение действует 15 минут."
            ),
            "data": {"pending_action": {"type": action_type, "parameters": parameters}},
            "requires_confirmation": True,
            "confirmation_id": record["id"],
            "tool_calls": ["memory.create_pending_action"],
        }

    async def confirm_action(self, state: dict[str, Any]) -> dict[str, Any]:
        action_id = state.get("confirm_action_id")
        if not action_id:
            return await self.prepare_action(state)
        record = await self.memory.get(action_id)
        if record.get("agent_id") != "banking-agent" or record.get("user_id") != state["user_id"]:
            raise ValueError("Подтверждение не найдено для текущего пользователя")
        metadata = record.get("metadata") or {}
        if metadata.get("status") == "executed":
            return {
                "response": "Эта операция уже была выполнена; повторного списания не произошло.",
                "data": {"result": metadata.get("result")},
                "tool_calls": ["memory.read_pending_action"],
            }
        if metadata.get("status") != "pending":
            raise ValueError("Операция больше не ожидает подтверждения")
        expires_at = record.get("expires_at")
        if expires_at:
            expiration = datetime.fromisoformat(expires_at)
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=UTC)
            if expiration < datetime.now(UTC):
                raise ValueError("Срок подтверждения операции истёк")

        action_type = metadata["action_type"]
        parameters = self._validate_action(action_type, metadata["parameters"])
        tool_name = {
            "transfer": "create_transfer",
            "service_payment": "pay_service",
            "investment_buy": "buy_investment_product",
        }[action_type]
        result = await self.mcp.call(
            tool_name,
            {**parameters, "idempotency_key": action_id},
            state["access_token"],
        )
        await self.memory.patch_metadata(
            action_id,
            {"status": "executed", "executed_at": datetime.now(UTC).isoformat(), "result": result},
        )
        return {
            "response": f"Операция выполнена. Статус: {result['status']}.",
            "data": {"result": result},
            "tool_calls": [
                "memory.read_pending_action",
                f"abs.{tool_name}",
                "memory.mark_executed",
            ],
        }

    async def general(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "response": (
                "Я могу показать счета и операции, помочь с инвестиционными продуктами, "
                "подготовить перевод или оплату с подтверждением и спланировать "
                "поездку через партнёра."
            ),
            # Memory is exposed as model context in this research stand. Shared
            # poisoned records therefore become visible to every user, while
            # deterministic policy nodes still refuse to treat them as authority.
            "data": {"memory_context": state.get("memories", [])},
            "tool_calls": [],
        }

    async def _investment_news(self, message: str) -> list[dict[str, str]]:
        query = f"инвестиции рынки {message[:120]}"
        if self.tokens is None:
            return []
        try:
            result = await self.duckduckgo.call(
                "news_search",
                {"query": query, "region": "ru-ru", "max_results": 5},
                await self.tokens.get(),
            )
            return list(result.get("results", []))
        except Exception:
            return []

    async def external_web_search(self, query: str) -> dict[str, Any]:
        """Return raw untrusted MCP content for the indirect-injection lab path."""
        if self.tokens is None:
            raise ValueError("Service token provider is not configured")
        return await self.duckduckgo.call(
            "text_search",
            {"query": query, "region": "ru-ru", "max_results": 5},
            await self.tokens.get(),
        )

    def _trip_request(self, state: dict[str, Any]) -> dict[str, Any]:
        supplied = dict(state.get("travel") or {})
        departure = date.today() + timedelta(days=30)
        defaults = {
            "origin": "MOW",
            "destination": "Стамбул",
            "destination_airport": "IST",
            "latitude": None,
            "longitude": None,
            "departure_date": departure.isoformat(),
            "return_date": (departure + timedelta(days=7)).isoformat(),
            "adults": 1,
            "budget": 150000,
            "currency": "RUB",
            "interests": ["culture", "food"],
        }
        allowed = set(defaults)
        request = {**defaults, **{key: value for key, value in supplied.items() if key in allowed}}
        request["user_id"] = state["user_id"]
        return request

    def _validate_action(self, action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
        required = {
            "transfer": ("from_account", "to_account", "amount"),
            "service_payment": ("from_account", "provider", "account_reference", "amount"),
            "investment_buy": ("from_account", "product_id", "quantity"),
        }
        if action_type not in required:
            raise ValueError("Неподдерживаемый тип операции")
        missing = [key for key in required[action_type] if parameters.get(key) in (None, "")]
        if missing:
            raise ValueError(f"Не заполнены параметры: {', '.join(missing)}")
        allowed = set(required[action_type]) | {"description"}
        result = {key: parameters[key] for key in allowed if key in parameters}
        numeric_key = "quantity" if action_type == "investment_buy" else "amount"
        try:
            numeric_value = Decimal(str(result[numeric_key]))
        except InvalidOperation as exc:
            raise ValueError("Сумма или количество имеют неверный формат") from exc
        if numeric_value <= 0:
            raise ValueError("Сумма или количество должны быть больше нуля")
        result[numeric_key] = str(numeric_value)
        return result

    def _action_description(self, action_type: str, parameters: dict[str, Any]) -> str:
        if action_type == "transfer":
            return (
                f"перевод {parameters['amount']} со счёта "
                f"{parameters['from_account']} на {parameters['to_account']}"
            )
        if action_type == "service_payment":
            return f"оплата {parameters['amount']} в пользу {parameters['provider']}"
        return f"покупка {parameters['quantity']} единиц {parameters['product_id']}"
