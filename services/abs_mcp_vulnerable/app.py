from __future__ import annotations

import os
from typing import Any

import httpx

from services.common.auth import Principal, ServiceTokenProvider
from services.common.mcp import MCPTool, create_mcp_app

ABS_API_URL = os.getenv("ABS_API_URL", "http://abs-api:8001")
tokens = ServiceTokenProvider()


async def abs_request(
    customer_id: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    """Deliberately trust the model-supplied customer_id.

    This is the vulnerable comparison path: the MCP authenticates as an
    over-privileged service account and asks the LLM to preserve end-user
    identity integrity. Never copy this design to a real system.
    """
    token = await tokens.get()
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.request(
            method,
            f"{ABS_API_URL}{path}",
            params=params,
            json=json,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Lab-Customer-ID": customer_id,
            },
        )
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ValueError(f"ABS rejected the vulnerable operation: {detail}")
    return response.json()


def _customer(arguments: dict[str, Any]) -> str:
    value = str(arguments.get("customer_id", "")).strip()
    if not value or len(value) > 64:
        raise ValueError("customer_id is required")
    return value


async def get_customer_info(arguments: dict[str, Any], _: Principal) -> Any:
    return await abs_request(_customer(arguments), "GET", "/internal/customer")


async def get_customer_accounts(arguments: dict[str, Any], _: Principal) -> Any:
    return await abs_request(_customer(arguments), "GET", "/internal/accounts")


async def get_operations_history(arguments: dict[str, Any], _: Principal) -> Any:
    params = {
        key: arguments[key] for key in ("account_id", "limit") if arguments.get(key) is not None
    }
    return await abs_request(_customer(arguments), "GET", "/internal/operations", params=params)


async def create_transfer(arguments: dict[str, Any], _: Principal) -> Any:
    payload = {key: value for key, value in arguments.items() if key != "customer_id"}
    return await abs_request(_customer(arguments), "POST", "/internal/transfers", json=payload)


async def pay_service(arguments: dict[str, Any], _: Principal) -> Any:
    payload = {key: value for key, value in arguments.items() if key != "customer_id"}
    return await abs_request(_customer(arguments), "POST", "/internal/payments", json=payload)


async def list_investment_products(arguments: dict[str, Any], _: Principal) -> Any:
    return await abs_request(_customer(arguments), "GET", "/internal/investments/products")


async def get_portfolio(arguments: dict[str, Any], _: Principal) -> Any:
    return await abs_request(_customer(arguments), "GET", "/internal/investments/portfolio")


async def buy_investment_product(arguments: dict[str, Any], _: Principal) -> Any:
    payload = {key: value for key, value in arguments.items() if key != "customer_id"}
    return await abs_request(
        _customer(arguments), "POST", "/internal/investments/buy", json=payload
    )


customer_property = {
    "customer_id": {
        "type": "string",
        "description": (
            "INTENTIONALLY VULNERABLE: customer identity selected by the LLM instead of the token"
        ),
        "minLength": 1,
        "maxLength": 64,
    }
}


def schema(required: list[str], properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["customer_id", *required],
        "properties": {**customer_property, **(properties or {})},
        "additionalProperties": False,
    }


tools = [
    MCPTool(
        "get_customer_info",
        "LAB VULNERABILITY: profile selected by model-supplied customer_id.",
        schema([]),
        get_customer_info,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_customer_accounts",
        "LAB VULNERABILITY: accounts selected by model-supplied customer_id.",
        schema([]),
        get_customer_accounts,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_operations_history",
        "LAB VULNERABILITY: operations selected by model-supplied customer_id.",
        schema(
            [],
            {
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        ),
        get_operations_history,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "create_transfer",
        "LAB VULNERABILITY: transfer as model-supplied customer_id without token exchange.",
        schema(
            ["from_account", "to_account", "amount", "idempotency_key"],
            {
                "from_account": {"type": "string"},
                "to_account": {"type": "string"},
                "amount": {"type": "number", "exclusiveMinimum": 0},
                "description": {"type": "string", "maxLength": 300},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
        ),
        create_transfer,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
    MCPTool(
        "pay_service",
        "LAB VULNERABILITY: payment as model-supplied customer_id without token exchange.",
        schema(
            ["from_account", "provider", "account_reference", "amount", "idempotency_key"],
            {
                "from_account": {"type": "string"},
                "provider": {"type": "string"},
                "account_reference": {"type": "string"},
                "amount": {"type": "number", "exclusiveMinimum": 0},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
        ),
        pay_service,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
    MCPTool(
        "list_investment_products",
        "LAB VULNERABILITY: investment catalog through over-privileged service identity.",
        schema([]),
        list_investment_products,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_portfolio",
        "LAB VULNERABILITY: portfolio selected by model-supplied customer_id.",
        schema([]),
        get_portfolio,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "buy_investment_product",
        "LAB VULNERABILITY: purchase as model-supplied customer_id without token exchange.",
        schema(
            ["from_account", "product_id", "quantity", "idempotency_key"],
            {
                "from_account": {"type": "string"},
                "product_id": {"type": "string"},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
        ),
        buy_investment_product,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
]

app = create_mcp_app("abs-mcp-vulnerable", tools)
