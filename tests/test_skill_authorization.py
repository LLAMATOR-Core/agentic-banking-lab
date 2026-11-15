"""Real signatures, API dependencies and database calls; no Docker/LLM required."""

import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from services.common import auth
from services.common.oauth_config import CLIENT_SCOPES
from services.common.skill_contract import Experiment, SkillMemoryCandidate, SkillMemoryRequest


@asynccontextmanager
async def no_startup(app):
    yield


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(auth._verifier, "_keys", {"test": key.public_key()})
    monkeypatch.setattr(auth._verifier, "_expires_at", float("inf"))

    def token(azp, aud, scope="portfolio.read", customer="customer-001"):
        import time

        return jwt.encode(
            {
                "iss": auth._verifier.issuer,
                "sub": "test-user",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "azp": azp,
                "aud": aud,
                "scope": scope,
                "customer_id": customer,
            },
            key,
            algorithm="RS256",
            headers={"kid": "test"},
        )

    return token


@pytest.fixture
def investment_mcp(monkeypatch):
    monkeypatch.setenv("SERVICE_CLIENT_ID", "bank-mcp")
    monkeypatch.setenv("SERVICE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_AZP", "investment-skill")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "bank-mcp")
    # Load a separate module to avoid mutating the legacy MCP used by other tests.
    import importlib.util

    spec = importlib.util.spec_from_file_location("investment_mcp_test", "services/abs_mcp/app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rpc(client, token, tool):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        },
    )


def test_investment_mcp_exposes_and_executes_only_portfolio(investment_mcp, signed, monkeypatch):
    module = investment_mcp
    calls = []

    async def exchange(token, *, scope):
        assert scope == "portfolio.read"
        calls.append(token)
        return "exchanged-test-token"

    async def request(*args, **kwargs):
        return httpx.Response(200, json=[{"product_id": "test"}])

    monkeypatch.setattr(module.token_exchange, "exchange", exchange)
    # Patch the method, preserving transport used by the TestClient itself.
    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    token = signed("investment-skill", "bank-mcp")
    with TestClient(module.app) as client:
        listed = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).json()
        assert [t["name"] for t in listed["result"]["tools"]] == ["get_portfolio"]
        assert rpc(client, token, "get_portfolio").json()["result"]["isError"] is False
        for forbidden in [
            "get_operations_history",
            "get_account_statement",
            "get_customer_accounts",
            "get_customer_info",
            "create_transfer",
            "buy_investment_product",
            "list_investment_products",
        ]:
            assert rpc(client, token, forbidden).status_code == 403
        for bad in [
            signed("investment-skill", "bank-mcp", "operations.read"),
            signed("operations-skill", "bank-mcp"),
            signed("investment-skill", "abs-api"),
        ]:
            assert rpc(client, bad, "get_portfolio").status_code == 403
    assert calls == [token]


@pytest.mark.asyncio
async def test_exchange_requests_scope_and_never_traces_credentials(monkeypatch):
    observations = []
    requests = []

    @contextmanager
    def observe(name, **kwargs):
        observations.append((name, kwargs))
        yield SimpleNamespace(update=lambda **updates: observations.append(updates))

    async def post(self, url, data):
        requests.append(data)
        return httpx.Response(
            200, json={"access_token": "OUTPUT_SECRET", "scope": "portfolio.read", "expires_in": 60}
        )

    monkeypatch.setattr(auth, "observation", observe)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    provider = auth.TokenExchangeProvider(
        "investment-skill", "CLIENT_SECRET", "bank-mcp", "portfolio.read"
    )
    assert await provider.exchange("INPUT_SECRET") == "OUTPUT_SECRET"
    assert requests[0]["scope"] == "portfolio.read"
    assert requests[0]["audience"] == "bank-mcp"
    wire = json.dumps(observations)
    assert all(value not in wire for value in ["INPUT_SECRET", "OUTPUT_SECRET", "CLIENT_SECRET"])
    provider.scope = "operations.read"
    with pytest.raises(ValueError, match="authority"):
        await provider.exchange("INPUT_SECRET")


def test_abs_enforces_scope_resource_and_customer_independently(signed, monkeypatch):
    from services.abs_service import app as abs_api

    monkeypatch.setenv("ALLOWED_AZP", "bank-mcp")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "abs-api")
    session = SimpleNamespace(scalars=None)
    calls = []

    async def scalars(query):
        calls.append(str(query.compile(compile_kwargs={"literal_binds": True})))
        return SimpleNamespace(all=lambda: [])

    session.scalars = scalars
    session.execute = scalars
    abs_api.app.dependency_overrides[abs_api.get_session] = lambda: session
    monkeypatch.setattr(abs_api.app.router, "lifespan_context", no_startup)
    try:
        with TestClient(abs_api.app) as client:
            for customer in ["customer-001", "customer-002"]:
                token = signed("bank-mcp", "abs-api", customer=customer)
                headers = {"Authorization": f"Bearer {token}", "X-Lab-Customer-ID": "victim"}
                assert (
                    client.get("/internal/investments/portfolio", headers=headers).status_code
                    == 200
                )
                assert customer in calls[-1] and "victim" not in calls[-1]
                for path in [
                    "/internal/operations",
                    "/internal/statement",
                    "/internal/accounts",
                    "/internal/customer",
                    "/internal/investments/products",
                ]:
                    assert client.get(path, headers=headers).status_code == 403
                payload = {
                    "from_account": "x",
                    "to_account": "y",
                    "amount": "1.00",
                    "idempotency_key": "test-denied-write",
                }
                assert (
                    client.post("/internal/transfers", headers=headers, json=payload).status_code
                    == 403
                )
            bad = signed("bank-mcp", "abs-api", scope="operations.read")
            assert (
                client.get(
                    "/internal/investments/portfolio", headers={"Authorization": f"Bearer {bad}"}
                ).status_code
                == 403
            )
        assert len(calls) == 2  # All refusals happen before database reads/writes.
    finally:
        abs_api.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_shared_memory_binds_identity_and_isolation_changes_reader_set(monkeypatch):
    from services.memory_service import app as mem

    reads, writes = [], []

    async def recall(payload, caller):
        reads.append(payload.model_dump())
        return {"memories": []}

    async def stage(payload, caller):
        writes.append(payload.model_dump())
        return {"job_id": "test-job"}

    monkeypatch.setattr(mem, "enrich_context", recall)
    monkeypatch.setattr(mem, "stage_candidate", stage)
    payload = SkillMemoryRequest(
        query="test", conversation_id="session-test", experiment=Experiment(run_id="run-001")
    )
    for mode in ["shared", "isolated"]:
        monkeypatch.setenv("SKILL_MEMORY_MODE", mode)
        for actor in ["operations-skill", "investment-skill", "travel-skill"]:
            caller = auth.Principal("subject", "user", "customer-001", actor, {})
            await mem.skill_context(payload, caller)
            await mem.skill_candidate(
                SkillMemoryCandidate(**payload.model_dump(), content="canary", source="internet"),
                caller,
            )
            assert reads[-1]["user_id"] == "customer-001"
            assert writes[-1]["metadata"]["writer_agent"] == actor
            assert writes[-1]["metadata"]["run_id"] == "run-001"
        assert len({r["agent_id"] for r in reads[-3:]}) == (1 if mode == "shared" else 3)


def test_skill_memory_rejects_scope_missing_customer_and_legacy_paths(signed, monkeypatch):
    from services.memory_service import app as mem

    monkeypatch.setenv("ALLOWED_AZP", "investment-skill")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "memory-service")
    body = {"query": "test", "conversation_id": "session-test", "experiment": {"run_id": "run"}}
    monkeypatch.setattr(mem.app.router, "lifespan_context", no_startup)
    with TestClient(mem.app) as client:
        for token in [
            signed("investment-skill", "memory-service", "portfolio.read"),
            signed("investment-skill", "memory-service", "memory.access", None),
        ]:
            assert (
                client.post(
                    "/api/v1/skills/context",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 403
            )
        good = signed("investment-skill", "memory-service", "memory.access")
        headers = {"Authorization": f"Bearer {good}"}
        assert client.post("/api/v1/context", json={}, headers=headers).status_code == 403
        assert (
            client.post(
                "/api/v1/skills/context", json={**body, "user_id": "victim"}, headers=headers
            ).status_code
            == 422
        )


def test_imported_realm_matches_scope_ceilings():
    from pathlib import Path

    realm = json.loads(Path("infra/keycloak/agentic-banking-realm.json").read_text())
    clients = {c["clientId"]: c for c in realm["clients"]}
    for identity, scopes in CLIENT_SCOPES.items():
        client = clients[identity]
        assert client["optionalClientScopes"] == scopes
        assert client["defaultClientScopes"] == []
        assert client["attributes"]["standard.token.exchange.enabled"] == "true"
    assert "operations.read" not in CLIENT_SCOPES["investment-skill"]


def test_imported_realm_emits_subject_without_granting_extra_resource_scopes():
    from pathlib import Path

    from services.common.oauth_config import TOKEN_CLIENTS, subject_mapper

    realm = json.loads(Path("infra/keycloak/agentic-banking-realm.json").read_text())
    clients = {client["clientId"]: client for client in realm["clients"]}
    for name in TOKEN_CLIENTS:
        assert subject_mapper() in clients[name]["protocolMappers"]
    assert {"profile", "email"} <= set(clients["bank-ui"]["optionalClientScopes"])
    identity_scopes = [s for s in realm["clientScopes"] if s["name"] in {"profile", "email"}]
    assert len(identity_scopes) == 2
    assert not any(
        m["protocolMapper"] == "oidc-audience-mapper"
        for scope in identity_scopes
        for m in scope.get("protocolMappers", [])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["operations", "investments", "travel"])
async def test_each_local_agent_uses_only_its_sources_and_records_memory_jobs(kind, monkeypatch):
    from services.banking_agent.agents import SkillAgent

    worker = SkillAgent(kind)
    assert worker.exchange.client_id == worker.identity
    assert worker.memory.exchange.client_id == worker.identity
    assert worker.tokens.client_id == worker.identity
    assert worker.exchange.audience == "bank-mcp"
    calls, staged, generations = [], [], []

    async def exchange(token):
        assert token == "DELEGATED_TOKEN"
        return "SCOPED_MCP_TOKEN"

    async def bank_call(name, args, token):
        calls.append((name, token))
        return [{"source": name}]

    async def search_call(name, args, token):
        calls.append((name, token))
        assert "PRIVATE_MEMORY_CANARY" not in json.dumps(args)
        return {"results": [{"body": "UNTRUSTED_WEB_CANARY"}]}

    async def get_token():
        return "SERVICE_TOKEN"

    async def memory_request(operation, payload, token):
        assert token == "DELEGATED_TOKEN"
        if operation == "context":
            return {
                "memory_mode": "shared",
                "memories": [{"id": "recalled-001", "content": "PRIVATE_MEMORY_CANARY"}],
            }
        if operation == "history":
            return {"messages": []}
        if operation == "candidates":
            staged.append(payload)
        return {"job_id": "job-test"}

    async def generate(schema, **kwargs):
        generations.append(kwargs)
        if kwargs["operation"] == "extract_statement_query":
            return schema()
        return schema(message="Ответ исполнителя")

    async def plan(request, conversation_id):
        calls.append(("a2a", request))
        assert "PRIVATE_MEMORY_CANARY" not in json.dumps(request)
        return {"agent_summary": "Рекомендации"}

    monkeypatch.setattr(worker.exchange, "exchange", exchange)
    monkeypatch.setattr(worker.bank, "call", bank_call)
    monkeypatch.setattr(worker.search, "call", search_call)
    monkeypatch.setattr(worker.tokens, "get", get_token)
    monkeypatch.setattr(worker.memory, "request", memory_request)
    monkeypatch.setattr(worker.llm, "generate_json", generate)
    if worker.partner:
        monkeypatch.setattr(worker.partner, "plan", plan)
    from services.common.skill_contract import SkillTask

    result = await worker.run(
        SkillTask(
            message="Задача",
            conversation_id="session-test",
            experiment=Experiment(run_id="run-001"),
            travel={"request_type": "destination_info", "destination": "Стамбул"}
            if kind == "travel"
            else None,
        ),
        user_id="customer-001",
        subject_token="DELEGATED_TOKEN",
    )
    assert [c[0] for c in calls] == {
        "operations": [
            "get_customer_info",
            "get_customer_accounts",
            "get_operations_history",
            "get_portfolio",
            "list_investment_products",
            "get_account_statement",
        ],
        "investments": ["get_portfolio", "news_search"],
        "travel": ["a2a"],
    }[kind]
    assert generations[-1]["payload"]["memory_context"][0]["content"] == "PRIVATE_MEMORY_CANARY"
    assert result["data"]["experiment"]["recalled_memory_ids"] == ["recalled-001"]
    assert all(p["experiment"]["run_id"] == "run-001" for p in staged)


@pytest.mark.asyncio
async def test_router_invokes_agent_locally_without_loading_bank_context(monkeypatch):
    from services.banking_agent import orchestrator

    routed = []

    async def generate(schema, **kwargs):
        routed.append(kwargs)
        if schema is orchestrator.Routing:
            return schema(intent="investments")
        return schema(response="Ответ оркестратора")

    async def last_skill(*args):
        return "operations"

    async def store_turn(**kwargs):
        assert kwargs["assistant_message"] == "Ответ оркестратора"

    async def history(*args):
        return []

    async def local_run(task, *, user_id, subject_token):
        assert user_id == "customer-001" and subject_token == "ORIGINAL_TOKEN"
        assert task.experiment.run_id == "run-001"
        return {"response": "Portfolio", "data": {}, "tool_calls": []}

    async def no_http(*args, **kwargs):
        raise AssertionError("Agent dispatch must stay inside the banking process")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_http)
    graph = orchestrator.OrchestratorGraph(
        SimpleNamespace(generate_json=generate),
        SimpleNamespace(
            last_skill=last_skill, store_turn=store_turn, conversation_messages=history
        ),
        agents={"investments": SimpleNamespace(identity="investment-skill", run=local_run)},
    )
    result = await graph.ainvoke(
        {
            "message": "Портфель",
            "user_id": "customer-001",
            "conversation_id": "session-test",
            "access_token": "ORIGINAL_TOKEN",
            "experiment": {"run_id": "run-001"},
        }
    )
    assert result["response"] == "Ответ оркестратора" and len(routed) == 2
    assert routed[0]["payload"]["previous_skill"] == "operations"
    assert "agent_results" not in routed[0]["payload"]
    assert routed[1]["payload"]["agent_results"]["investments"]["response"] == "Portfolio"


@pytest.mark.asyncio
async def test_skill_promotions_preserve_customer_and_provenance(monkeypatch):
    from services.memory_service import orchestrator as control

    writes = []

    async def insert(**kwargs):
        writes.append(kwargs)
        return kwargs

    monkeypatch.setattr(control, "_insert_memory", insert)
    promotions = [
        control.MemoryPromotion(memory_type=kind, content="canary", rationale="test")
        for kind in ["semantic", "procedural", "episodic"]
    ]
    await control._persist_promotions(
        "skills.shared",
        "customer-001",
        promotions,
        source={
            "derived_from_job": "job-001",
            "writer_agent": "operations-skill",
            "run_id": "run-001",
        },
        created_by="memory-orchestrator",
    )
    assert all(row["user_id"] == "customer-001" for row in writes)
    assert all(row["metadata"]["derived_from_job"] == "job-001" for row in writes)
    assert all(row["metadata"]["writer_agent"] == "operations-skill" for row in writes)


@pytest.mark.asyncio
async def test_agents_use_their_own_credentials_for_all_downstreams(monkeypatch):
    import asyncio

    from services.banking_agent.agents import SkillAgent

    requests = []

    async def post(self, url, data):
        await asyncio.sleep(0)  # Interleave users and agents to expose shared mutable providers.
        requests.append(data.copy())
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "access_token": (
                    f"{data['client_id']}:{data.get('subject_token', 'service')}:"
                    f"{data.get('scope')}"
                ),
                "scope": data.get("scope", ""),
                "expires_in": 60,
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    agents = [SkillAgent(kind) for kind in ("operations", "investments", "travel")]
    work = []
    for agent in agents:
        for user in ("USER_A", "USER_B"):
            work.append(agent.memory.exchange.exchange(user))
            if agent.kind != "travel":
                work.append(agent.exchange.exchange(user))
        work.append(agent.tokens.get())
    results = await asyncio.gather(*work)
    assert len(set(results)) == len(results)
    for request in requests:
        assert request["client_secret"] == request["client_id"] + "-secret"
        if request["grant_type"] == "client_credentials":
            if request["client_id"] == "travel-skill":
                assert request["scope"] == "travel.plan"
        else:
            assert request["subject_token"] in {"USER_A", "USER_B"}
            if request["audience"] == "memory-service":
                assert request["scope"] == "memory.access"
            else:
                assert request["audience"] == "bank-mcp"
                assert (
                    request["scope"]
                    == {
                        "operations-skill": agents[0].scope,
                        "investment-skill": "portfolio.read",
                    }[request["client_id"]]
                )


def test_shared_mcp_filters_tools_by_agent_and_scope(investment_mcp, signed, monkeypatch):
    module = investment_mcp
    monkeypatch.setenv("ALLOWED_AZP", "operations-skill,investment-skill")
    calls = []

    async def abs_request(principal, method, path, **kwargs):
        calls.append((principal.authorized_party, path))
        return []

    monkeypatch.setattr(module, "abs_request", abs_request)
    with TestClient(module.app) as client:
        for actor, scope, tool, listed_tools in [
            (
                "operations-skill",
                "operations.read",
                "get_operations_history",
                {"get_operations_history", "get_account_statement"},
            ),
            ("investment-skill", "portfolio.read", "get_portfolio", {"get_portfolio"}),
        ]:
            token = signed(actor, "bank-mcp", scope)
            listed = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                },
            ).json()["result"]["tools"]
            assert {entry["name"] for entry in listed} == listed_tools
            assert rpc(client, token, tool).json()["result"]["isError"] is False
            other = (
                "get_portfolio" if tool == "get_operations_history" else "get_operations_history"
            )
            assert rpc(client, token, other).status_code == 403
        invalid = signed("investment-skill", "bank-mcp", "operations.read")
        assert rpc(client, invalid, "get_operations_history").status_code == 403
        from services.common.banking_policy import AGENT_SCOPES, TOOL_SCOPES

        full = signed("operations-skill", "bank-mcp", " ".join(AGENT_SCOPES["operations-skill"]))
        listed = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {full}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ).json()["result"]["tools"]
        assert {t["name"] for t in listed} == {
            name for name, scope in TOOL_SCOPES.items() if scope != "bank.write"
        }
        # The role ceiling still denies writes even with a forged excessive scope claim.
        excessive = signed("operations-skill", "bank-mcp", "bank.write")
        assert rpc(client, excessive, "create_transfer").status_code == 403
    assert calls == [
        ("operations-skill", "/internal/operations"),
        ("investment-skill", "/internal/investments/portfolio"),
    ]
