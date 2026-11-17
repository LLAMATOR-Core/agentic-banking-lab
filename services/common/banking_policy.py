"""Permissions enforced by the shared banking MCP and again by the ABS API."""

TOOL_SCOPES = {
    "get_customer_info": "customer.read",
    "get_customer_accounts": "accounts.read",
    "get_operations_history": "operations.read",
    "get_account_statement": "operations.read",
    "get_portfolio": "portfolio.read",
    "list_investment_products": "products.read",
    "create_transfer": "bank.write",
    "pay_service": "bank.write",
    "buy_investment_product": "bank.write",
}
RESOURCE_SCOPES = {
    ("GET", "/internal/customer"): "customer.read",
    ("GET", "/internal/accounts"): "accounts.read",
    ("GET", "/internal/operations"): "operations.read",
    ("GET", "/internal/statement"): "operations.read",
    ("GET", "/internal/investments/portfolio"): "portfolio.read",
    ("GET", "/internal/investments/products"): "products.read",
    ("POST", "/internal/transfers"): "bank.write",
    ("POST", "/internal/payments"): "bank.write",
    ("POST", "/internal/investments/buy"): "bank.write",
}
AGENT_SCOPES = {
    "operations-skill": frozenset(
        {
            "customer.read",
            "accounts.read",
            "operations.read",
            "portfolio.read",
            "products.read",
        }
    ),
    "investment-skill": frozenset({"portfolio.read"}),
}
