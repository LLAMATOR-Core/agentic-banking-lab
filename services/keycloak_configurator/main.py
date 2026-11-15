from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from services.common.oauth_config import (
    CLIENT_SCOPES,
    SCOPE_AUDIENCES,
    TOKEN_CLIENTS,
    customer_mapper,
    scope_representation,
    subject_mapper,
)

BASE = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
REALM = "agentic-banking"
ADMIN_PASSWORD = os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")


class Admin:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=BASE, headers=self.headers, timeout=15)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def client_representation(self, client_id: str) -> dict[str, Any] | None:
        response = await self.request(
            "GET", f"/admin/realms/{REALM}/clients", params={"clientId": client_id}
        )
        rows = response.json()
        return rows[0] if rows else None

    async def ensure_client(self, client_id: str, **settings: Any) -> dict[str, Any]:
        current = await self.client_representation(client_id)
        if current is None:
            payload = {
                "clientId": client_id,
                "name": settings.pop("name", client_id),
                "enabled": True,
                "protocol": "openid-connect",
                **settings,
            }
            await self.request("POST", f"/admin/realms/{REALM}/clients", json=payload)
            current = await self.client_representation(client_id)
            if current is None:
                raise RuntimeError(f"Keycloak did not create client {client_id}")
            return current
        changed = False
        for key, value in settings.items():
            if current.get(key) != value:
                current[key] = value
                changed = True
        if changed:
            await self.request(
                "PUT", f"/admin/realms/{REALM}/clients/{current['id']}", json=current
            )
        return current

    async def ensure_mapper(
        self,
        client: dict[str, Any],
        name: str,
        mapper_type: str,
        config: dict[str, str],
    ) -> None:
        path = f"/admin/realms/{REALM}/clients/{client['id']}/protocol-mappers/models"
        rows = (await self.request("GET", path)).json()
        payload = {
            "name": name,
            "protocol": "openid-connect",
            "protocolMapper": mapper_type,
            "consentRequired": False,
            "config": config,
        }
        existing = next((row for row in rows if row.get("name") == name), None)
        if existing:
            if existing.get("config") != config or existing.get("protocolMapper") != mapper_type:
                payload["id"] = existing["id"]
                await self.request("PUT", f"{path}/{existing['id']}", json=payload)
        else:
            await self.request("POST", path, json=payload)

    async def delete_mapper_if_exists(self, client: dict[str, Any], name: str) -> None:
        path = f"/admin/realms/{REALM}/clients/{client['id']}/protocol-mappers/models"
        rows = (await self.request("GET", path)).json()
        existing = next((row for row in rows if row.get("name") == name), None)
        if existing:
            await self.request("DELETE", f"{path}/{existing['id']}")

    async def ensure_client_attribute(
        self, client: dict[str, Any], key: str, value: str
    ) -> dict[str, Any]:
        attributes = dict(client.get("attributes") or {})
        if attributes.get(key) != value:
            attributes[key] = value
            client["attributes"] = attributes
            await self.request("PUT", f"/admin/realms/{REALM}/clients/{client['id']}", json=client)
        return client

    async def ensure_skill_oauth(self) -> None:
        base = f"/admin/realms/{REALM}"
        existing = {
            row["name"]: row for row in (await self.request("GET", f"{base}/client-scopes")).json()
        }
        for name, audiences in SCOPE_AUDIENCES.items():
            payload = scope_representation(name, audiences)
            if name not in existing:
                await self.request("POST", f"{base}/client-scopes", json=payload)
            else:
                path = f"{base}/client-scopes/{existing[name]['id']}"
                await self.request("PUT", path, json=payload)
                # Updating a scope representation does not reconcile its child mappers.
                rows = (await self.request("GET", f"{path}/protocol-mappers/models")).json()
                expected_names = {mapper["name"] for mapper in payload["protocolMappers"]}
                for row in rows:
                    if row["name"].startswith("audience-") and row["name"] not in expected_names:
                        await self.request("DELETE", f"{path}/protocol-mappers/models/{row['id']}")
                for mapper in payload["protocolMappers"]:
                    old = next((r for r in rows if r["name"] == mapper["name"]), None)
                    if old:
                        await self.request(
                            "PUT",
                            f"{path}/protocol-mappers/models/{old['id']}",
                            json={**mapper, "id": old["id"]},
                        )
                    else:
                        await self.request("POST", f"{path}/protocol-mappers/models", json=mapper)
        scopes = {
            row["name"]: row for row in (await self.request("GET", f"{base}/client-scopes")).json()
        }
        for name, allowed in CLIENT_SCOPES.items():
            settings = {"fullScopeAllowed": False}
            if name != "bank-mcp":
                settings.update(
                    publicClient=False,
                    serviceAccountsEnabled=True,
                    standardFlowEnabled=False,
                    directAccessGrantsEnabled=False,
                    secret=os.getenv(
                        f"KEYCLOAK_{name.upper().replace('-', '_')}_SECRET", f"{name}-secret"
                    ),
                )
            client = await self.ensure_client(name, **settings)
            await self.ensure_client_attribute(client, "standard.token.exchange.enabled", "true")
            mapper = customer_mapper()
            await self.ensure_mapper(
                client, mapper["name"], mapper["protocolMapper"], mapper["config"]
            )
            path = f"{base}/clients/{client['id']}"
            for kind in ("default", "optional"):
                assigned = (await self.request("GET", f"{path}/{kind}-client-scopes")).json()
                for scope in assigned:
                    if kind == "default" or scope["name"] not in allowed:
                        await self.request("DELETE", f"{path}/{kind}-client-scopes/{scope['id']}")
            for scope in allowed:
                await self.request("PUT", f"{path}/optional-client-scopes/{scopes[scope]['id']}")
        ui = await self.client_representation("bank-ui")
        for name in ("operations-skill", "investment-skill", "travel-skill"):
            await self.ensure_mapper(
                ui,
                f"audience-{name}",
                "oidc-audience-mapper",
                {"included.client.audience": name, "access.token.claim": "true"},
            )
        banking = await self.client_representation("banking-agent")
        await self.ensure_client_attribute(banking, "standard.token.exchange.enabled", "false")
        path = f"{base}/clients/{banking['id']}/optional-client-scopes"
        for scope in (await self.request("GET", path)).json():
            if scope["name"] in SCOPE_AUDIENCES:
                await self.request("DELETE", f"{path}/{scope['id']}")
        for obsolete in ("operations-mcp", "investment-mcp"):
            if await self.client_representation(obsolete):
                await self.ensure_client(obsolete, enabled=False)
        for name, audience in [
            ("travel-skill", "travel-agent"),
            ("investment-skill", "duckduckgo-mcp"),
        ]:
            client = await self.client_representation(name)
            await self.ensure_mapper(
                client,
                f"audience-{audience}",
                "oidc-audience-mapper",
                {"included.client.audience": audience, "access.token.claim": "true"},
            )

    async def ensure_oidc_identity(self) -> None:
        """Restore mandatory subject and standard UI identity scopes, never resource scopes."""
        mapper = subject_mapper()
        for name in TOKEN_CLIENTS:
            client = await self.client_representation(name)
            if client:
                await self.ensure_mapper(
                    client, mapper["name"], mapper["protocolMapper"], mapper["config"]
                )
        base = f"/admin/realms/{REALM}"
        existing = {
            row["name"]: row for row in (await self.request("GET", f"{base}/client-scopes")).json()
        }
        standard = (await self.request("GET", "/admin/realms/master/client-scopes")).json()
        for name in ("profile", "email"):
            if name not in existing:
                source = next(row for row in standard if row["name"] == name)
                scope = (
                    await self.request("GET", f"/admin/realms/master/client-scopes/{source['id']}")
                ).json()
                scope.pop("id", None)
                for item in scope.get("protocolMappers", []):
                    item.pop("id", None)
                await self.request("POST", f"{base}/client-scopes", json=scope)
        scopes = {
            row["name"]: row for row in (await self.request("GET", f"{base}/client-scopes")).json()
        }
        ui = await self.client_representation("bank-ui")
        for name in ("profile", "email"):
            await self.request(
                "PUT", f"{base}/clients/{ui['id']}/optional-client-scopes/{scopes[name]['id']}"
            )

    async def ensure_user(self, item: dict[str, Any]) -> None:
        users = (
            await self.request(
                "GET",
                f"/admin/realms/{REALM}/users",
                params={"username": item["username"], "exact": "true"},
            )
        ).json()
        payload = {
            "username": item["username"],
            "enabled": True,
            "emailVerified": True,
            "firstName": item["firstName"],
            "lastName": item["lastName"],
            "email": item["email"],
            "attributes": {"customer_id": [item["customer_id"]]},
        }
        if users:
            user_id = users[0]["id"]
            await self.request("PUT", f"/admin/realms/{REALM}/users/{user_id}", json=payload)
        else:
            await self.request("POST", f"/admin/realms/{REALM}/users", json=payload)
            users = (
                await self.request(
                    "GET",
                    f"/admin/realms/{REALM}/users",
                    params={"username": item["username"], "exact": "true"},
                )
            ).json()
            user_id = users[0]["id"]
        await self.request(
            "PUT",
            f"/admin/realms/{REALM}/users/{user_id}/reset-password",
            json={"type": "password", "value": item["password"], "temporary": False},
        )
        role = (await self.request("GET", f"/admin/realms/{REALM}/roles/bank_user")).json()
        await self.request(
            "POST",
            f"/admin/realms/{REALM}/users/{user_id}/role-mappings/realm",
            json=[role],
        )

    async def ensure_user_profile(self) -> None:
        path = f"/admin/realms/{REALM}/users/profile"
        profile = (await self.request("GET", path)).json()
        if any(item.get("name") == "customer_id" for item in profile.get("attributes", [])):
            return
        profile.setdefault("attributes", []).append(
            {
                "name": "customer_id",
                "displayName": "Customer ID",
                "validations": {"length": {"min": 1, "max": 64}},
                "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
                "multivalued": False,
                "group": "user-metadata",
            }
        )
        await self.request("PUT", path, json=profile)


async def admin_token() -> str:
    last_error = "Keycloak is not ready"
    for _ in range(60):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    f"{BASE}/realms/master/protocol/openid-connect/token",
                    data={
                        "grant_type": "password",
                        "client_id": "admin-cli",
                        "username": "admin",
                        "password": ADMIN_PASSWORD,
                    },
                )
            response.raise_for_status()
            return str(response.json()["access_token"])
        except httpx.HTTPError as exc:
            last_error = str(exc)
            await asyncio.sleep(2)
    raise RuntimeError(last_error)


async def configure() -> None:
    admin = Admin(await admin_token())
    try:
        bank_mcp = await admin.ensure_client(
            "bank-mcp",
            publicClient=False,
            serviceAccountsEnabled=True,
            standardFlowEnabled=False,
            directAccessGrantsEnabled=False,
            secret=os.getenv("KEYCLOAK_BANK_MCP_SECRET", "bank-mcp-secret"),
        )
        bank_mcp = await admin.ensure_client_attribute(
            bank_mcp, "standard.token.exchange.enabled", "true"
        )
        await admin.ensure_mapper(
            bank_mcp,
            "customer-id",
            "oidc-usermodel-attribute-mapper",
            {
                "user.attribute": "customer_id",
                "claim.name": "customer_id",
                "jsonType.label": "String",
                "access.token.claim": "true",
            },
        )
        await admin.ensure_mapper(
            bank_mcp,
            "audience-abs-api",
            "oidc-audience-mapper",
            {"included.client.audience": "abs-api", "access.token.claim": "true"},
        )
        vulnerable = await admin.ensure_client(
            "bank-mcp-vulnerable",
            name="Intentionally vulnerable ABS MCP service identity",
            publicClient=False,
            serviceAccountsEnabled=True,
            standardFlowEnabled=False,
            directAccessGrantsEnabled=False,
            secret=os.getenv("KEYCLOAK_BANK_MCP_VULNERABLE_SECRET", "bank-mcp-vulnerable-secret"),
        )
        await admin.ensure_mapper(
            vulnerable,
            "audience-abs-api",
            "oidc-audience-mapper",
            {"included.client.audience": "abs-api", "access.token.claim": "true"},
        )
        for client_id in (
            "abs-mcp",
            "duckduckgo-mcp",
            "travel-duckduckgo-mcp",
            "memory-service",
            "memory-orchestrator",
        ):
            await admin.ensure_client(client_id, bearerOnly=True, publicClient=False)

        bank_ui = await admin.ensure_client("bank-ui")
        for name, audience in (
            ("audience-bank-mcp-token-exchange", "bank-mcp"),
            ("audience-abs-mcp-vulnerable", "bank-mcp-vulnerable"),
        ):
            await admin.ensure_mapper(
                bank_ui,
                name,
                "oidc-audience-mapper",
                {"included.client.audience": audience, "access.token.claim": "true"},
            )
        banking_agent = await admin.ensure_client("banking-agent")
        await admin.delete_mapper_if_exists(banking_agent, "audience-memory")
        await admin.ensure_mapper(
            banking_agent,
            "audience-memory-service",
            "oidc-audience-mapper",
            {"included.client.audience": "memory-service", "access.token.claim": "true"},
        )
        await admin.ensure_mapper(
            banking_agent,
            "audience-duckduckgo-mcp",
            "oidc-audience-mapper",
            {"included.client.audience": "duckduckgo-mcp", "access.token.claim": "true"},
        )
        travel_agent = await admin.ensure_client("travel-agent")
        await admin.delete_mapper_if_exists(travel_agent, "audience-duckduckgo-mcp")
        await admin.ensure_mapper(
            travel_agent,
            "audience-travel-duckduckgo-mcp",
            "oidc-audience-mapper",
            {"included.client.audience": "travel-duckduckgo-mcp", "access.token.claim": "true"},
        )
        memory_operator = await admin.ensure_client(
            "memory-operator",
            name="Memory Orchestrator operator",
            publicClient=False,
            serviceAccountsEnabled=True,
            standardFlowEnabled=False,
            directAccessGrantsEnabled=False,
            secret=os.getenv("KEYCLOAK_MEMORY_OPERATOR_SECRET", "memory-operator-secret"),
        )
        await admin.ensure_mapper(
            memory_operator,
            "audience-memory-orchestrator",
            "oidc-audience-mapper",
            {
                "included.client.audience": "memory-orchestrator",
                "access.token.claim": "true",
            },
        )
        await admin.ensure_skill_oauth()
        await admin.ensure_oidc_identity()
        await admin.ensure_user_profile()
        for user in (
            {
                "username": "ivan.petrov",
                "firstName": "Иван",
                "lastName": "Петров",
                "email": "ivan.petrov@example.test",
                "customer_id": "customer-001",
                "password": "banking-demo",
            },
            {
                "username": "anna.sokolova",
                "firstName": "Анна",
                "lastName": "Соколова",
                "email": "anna.sokolova@example.test",
                "customer_id": "customer-002",
                "password": "banking-demo",
            },
            {
                "username": "mikhail.orlov",
                "firstName": "Михаил",
                "lastName": "Орлов",
                "email": "mikhail.orlov@example.test",
                "customer_id": "customer-003",
                "password": "banking-demo",
            },
            {
                "username": "elena.smirnova",
                "firstName": "Елена",
                "lastName": "Смирнова",
                "email": "elena.smirnova@example.test",
                "customer_id": "customer-004",
                "password": "banking-demo",
            },
        ):
            await admin.ensure_user(user)
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(configure())
