from __future__ import annotations

import importlib
import json
from pathlib import Path


def test_secure_and_vulnerable_abs_mcp_identity_contracts(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_CLIENT_ID", "test-client")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    secure = importlib.import_module("services.abs_mcp.app")
    vulnerable = importlib.import_module("services.abs_mcp_vulnerable.app")

    for tool in secure.tools:
        assert "customer_id" not in tool.input_schema.get("properties", {})
        assert "customer_id" not in tool.input_schema.get("required", [])

    for tool in vulnerable.tools:
        assert "customer_id" in tool.input_schema["properties"]
        assert "customer_id" in tool.input_schema["required"]


def test_realm_contains_token_exchange_providers_and_multiple_users() -> None:
    realm_path = Path("infra/keycloak/agentic-banking-realm.json")
    realm = json.loads(realm_path.read_text(encoding="utf-8"))
    clients = {item["clientId"]: item for item in realm["clients"]}
    assert clients["bank-mcp"]["attributes"]["standard.token.exchange.enabled"] == "true"
    assert "bank-mcp-vulnerable" in clients
    assert "duckduckgo-mcp" in clients

    users = {item["username"]: item for item in realm["users"]}
    assert {"ivan.petrov", "anna.sokolova", "mikhail.orlov", "elena.smirnova"} <= users.keys()
    customer_ids = {
        users[name]["attributes"]["customer_id"][0]
        for name in ("ivan.petrov", "anna.sokolova", "mikhail.orlov", "elena.smirnova")
    }
    assert customer_ids == {"customer-001", "customer-002", "customer-003", "customer-004"}
