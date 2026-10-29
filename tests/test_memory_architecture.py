from __future__ import annotations

import json
from pathlib import Path

from services.memory_service import app as data_plane
from services.memory_service import orchestrator as control_plane
from services.memory_service.core import EmbeddingClient, cosine_similarity


def _routes(application) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in application.routes
        for method in getattr(route, "methods", set())
    }


def test_memory_data_plane_only_writes_working_memory() -> None:
    routes = _routes(data_plane.app)
    assert ("POST", "/api/v1/context") in routes
    assert ("GET", "/api/v1/episodic/{agent_id}/{user_id}") in routes
    assert ("GET", "/api/v1/semantic/{agent_id}/{user_id}") in routes
    assert ("GET", "/api/v1/procedure/{agent_id}") in routes
    assert ("POST", "/api/v1/working/turns") in routes
    assert ("POST", "/api/v1/working/actions") in routes
    assert not any(
        method in {"POST", "PUT", "PATCH", "DELETE"}
        and path.startswith(("/api/v1/episodic", "/api/v1/semantic", "/api/v1/procedural"))
        for method, path in routes
    )


def test_orchestrator_owns_long_term_crud_and_collection() -> None:
    routes = _routes(control_plane.app)
    assert ("POST", "/api/v1/orchestrator/collect") in routes
    assert ("GET", "/api/v1/episodic") in routes
    assert ("GET", "/api/v1/episodic/{agent_id}") in routes
    assert ("GET", "/api/v1/episodic/{agent_id}/{user_id}") in routes
    assert ("GET", "/api/v1/semantic") in routes
    assert ("GET", "/api/v1/semantic/{agent_id}") in routes
    assert ("GET", "/api/v1/procedure/{agent_id}") in routes
    assert ("POST", "/api/v1/episodic/direct") in routes
    assert ("POST", "/api/v1/episodic/agent/collect/{thread_id}") in routes
    assert ("POST", "/api/v1/semantic") in routes
    assert ("POST", "/api/v1/procedural/{agent_id}") in routes
    assert ("PUT", "/api/v1/procedural/{agent_id}/{procedure_id}") in routes
    assert ("DELETE", "/api/v1/procedural/{agent_id}/{procedure_id}") in routes


def test_keycloak_separates_memory_service_and_orchestrator_audiences() -> None:
    realm = json.loads(
        Path("infra/keycloak/agentic-banking-realm.json").read_text(encoding="utf-8")
    )
    clients = {item["clientId"]: item for item in realm["clients"]}
    banking_audiences = {
        mapper["config"].get("included.client.audience")
        for mapper in clients["banking-agent"]["protocolMappers"]
    }
    operator_audiences = {
        mapper["config"].get("included.client.audience")
        for mapper in clients["memory-operator"]["protocolMappers"]
    }
    assert "memory-service" in banking_audiences
    assert "memory-orchestrator" not in banking_audiences
    assert operator_audiences == {"memory-orchestrator"}


def test_embedding_client_reuses_openai_base_url_and_separate_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.internal:11434/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "custom-embedding")
    client = EmbeddingClient("test-memory")

    assert str(client.client.base_url) == "http://llm.internal:11434/v1/"
    assert client.model == "custom-embedding"
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0], [1.0, 0.0]) == -1.0
