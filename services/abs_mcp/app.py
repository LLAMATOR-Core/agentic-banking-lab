from __future__ import annotations

import os
from typing import Any

import httpx

from services.common.auth import Principal, TokenExchangeProvider, require_scope
from services.common.banking_policy import AGENT_SCOPES, RESOURCE_SCOPES, TOOL_SCOPES
from services.common.mcp import MCPTool, create_mcp_app
from services.common.tracing import trace_headers

ABS_API_URL = os.getenv("ABS_API_URL", "http://abs-api:8001")
token_exchange = TokenExchangeProvider(audience="abs-api")


async def abs_request(
    principal: Principal,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    # The downstream identity is cryptographically bound by Keycloak. No
    # customer identifier is accepted from the model or copied into a header.
    scope = RESOURCE_SCOPES[(method, path)]
    if principal.authorized_party in AGENT_SCOPES:
        require_scope(principal, scope)
        if scope not in AGENT_SCOPES[principal.authorized_party]:
            raise ValueError("Resource is outside this agent's permissions")
    token = await token_exchange.exchange(principal.access_token, scope=scope)
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.request(
            method,
            f"{ABS_API_URL}{path}",
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}", **trace_headers()},
        )
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ValueError(f"ABS rejected the operation: {detail}")
    return response.json()


async def get_customer_info(_: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "GET", "/internal/customer")


async def get_customer_accounts(_: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "GET", "/internal/accounts")


async def get_operations_history(arguments: dict[str, Any], principal: Principal) -> Any:
    params = {
        key: arguments[key] for key in ("account_id", "limit") if arguments.get(key) is not None
    }
    return await abs_request(principal, "GET", "/internal/operations", params=params)


async def get_account_statement(arguments: dict[str, Any], principal: Principal) -> Any:
    params = {
        key: arguments[key]
        for key in ("account_id", "date_from", "date_to", "limit", "offset")
        if arguments.get(key) is not None
    }
    return await abs_request(principal, "GET", "/internal/statement", params=params)


async def create_transfer(arguments: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "POST", "/internal/transfers", json=arguments)


async def pay_service(arguments: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "POST", "/internal/payments", json=arguments)


async def list_investment_products(_: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "GET", "/internal/investments/products")


async def get_portfolio(_: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "GET", "/internal/investments/portfolio")


async def buy_investment_product(arguments: dict[str, Any], principal: Principal) -> Any:
    return await abs_request(principal, "POST", "/internal/investments/buy", json=arguments)


empty_schema = {"type": "object", "properties": {}, "additionalProperties": False}
tools = [
    MCPTool(
        "get_account_statement",
        "Read the customer's statement for an inclusive date range, with totals by currency.",
        {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "date_from": {"type": "string", "format": "date"},
                "date_to": {"type": "string", "format": "date"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1000},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
        get_account_statement,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_customer_info",
        "Return the authenticated customer's profile. Identity comes from an exchanged JWT.",
        empty_schema,
        get_customer_info,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_customer_accounts",
        "Return accounts and balances belonging to the authenticated customer.",
        empty_schema,
        get_customer_accounts,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_operations_history",
        "Return recent operations for the authenticated customer.",
        {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
        get_operations_history,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "create_transfer",
        "Execute a previously user-confirmed transfer. Requires a unique idempotency key.",
        {
            "type": "object",
            "required": ["from_account", "to_account", "amount", "idempotency_key"],
            "properties": {
                "from_account": {"type": "string"},
                "to_account": {"type": "string"},
                "amount": {"type": "number", "exclusiveMinimum": 0},
                "description": {"type": "string", "maxLength": 300},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
            "additionalProperties": False,
        },
        create_transfer,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
    MCPTool(
        "pay_service",
        "Pay a service bill after explicit user confirmation.",
        {
            "type": "object",
            "required": [
                "from_account",
                "provider",
                "account_reference",
                "amount",
                "idempotency_key",
            ],
            "properties": {
                "from_account": {"type": "string"},
                "provider": {"type": "string"},
                "account_reference": {"type": "string"},
                "amount": {"type": "number", "exclusiveMinimum": 0},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
            "additionalProperties": False,
        },
        pay_service,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
    MCPTool(
        "list_investment_products",
        "List investment products available in the bank.",
        empty_schema,
        list_investment_products,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "get_portfolio",
        "Return the authenticated customer's investment portfolio.",
        empty_schema,
        get_portfolio,
        {"readOnlyHint": True},
    ),
    MCPTool(
        "buy_investment_product",
        "Buy an investment product after explicit user confirmation.",
        {
            "type": "object",
            "required": ["from_account", "product_id", "quantity", "idempotency_key"],
            "properties": {
                "from_account": {"type": "string"},
                "product_id": {"type": "string"},
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "idempotency_key": {"type": "string", "minLength": 8},
            },
            "additionalProperties": False,
        },
        buy_investment_product,
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    ),
]


def tool_allowed(name: str, principal: Principal) -> bool:
    if principal.authorized_party in {"bank-ui", "debug"}:
        return True
    scope = TOOL_SCOPES[name]
    return (
        scope in AGENT_SCOPES.get(principal.authorized_party, ())
        and scope in str(principal.claims.get("scope", "")).split()
    )


app = create_mcp_app("abs-mcp-secure", tools, authorize=tool_allowed)
