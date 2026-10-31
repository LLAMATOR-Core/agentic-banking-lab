from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def services():
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is not installed")
    names = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in names | {"COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"}
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["services"]


def test_agent_systems_do_not_share_networks_or_database_access(services):
    roots = ["banking-agent", "travel-agent", "memory-orchestrator"]
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            assert not set(services[left]["networks"]) & set(services[right]["networks"])
    for caller, targets in {
        "banking-agent": [
            "travel-agent",
            "flights-mcp",
            "travel-duckduckgo-mcp",
            "memory-service",
            "memory-orchestrator",
            "mongo",
            "postgres",
        ],
        "travel-agent": [
            "banking-agent",
            "abs-api",
            "abs-mcp",
            "memory-service",
            "memory-orchestrator",
            "mongo",
            "postgres",
        ],
        "memory-orchestrator": [
            "banking-agent",
            "abs-api",
            "travel-agent",
            "flights-mcp",
            "postgres",
        ],
    }.items():
        for target in targets:
            assert not set(services[caller]["networks"]) & set(services[target]["networks"])
    assert "MONGODB_URL" not in services["banking-agent"]["environment"]
    assert "MONGODB_URL" not in services["travel-agent"]["environment"]
    assert "FLIGHTS_MCP_AUTH_TOKEN" not in services["banking-agent"]["environment"]
    assert services["banking-agent"]["environment"]["TRAVEL_AGENT_URL"] == "http://travel-api:8003"
    assert services["banking-agent"]["environment"]["MEMORY_URL"] == "http://memory-api:8002"


def test_each_system_exports_only_to_its_own_langfuse_project(services):
    systems = {
        "banking": (
            "langfuse-web",
            "Banking",
            ["banking-agent", "abs-api", "abs-mcp", "abs-mcp-vulnerable", "duckduckgo-mcp"],
        ),
        "travel": (
            "langfuse-travel",
            "Travel",
            ["travel-agent", "flights-mcp", "travel-duckduckgo-mcp"],
        ),
        "memory": (
            "langfuse-memory",
            "Persistent Memory",
            ["memory-service", "memory-orchestrator"],
        ),
    }
    public_keys, secret_keys, project_ids = set(), set(), set()
    for system, (collector, project, members) in systems.items():
        initialized = services[collector]["environment"]
        assert initialized["LANGFUSE_INIT_PROJECT_NAME"] == project
        project_ids.add(initialized["LANGFUSE_INIT_PROJECT_ID"])
        public_keys.add(initialized["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"])
        secret_keys.add(initialized["LANGFUSE_INIT_PROJECT_SECRET_KEY"])
        for service in members:
            env = services[service]["environment"]
            assert env["AGENT_SYSTEM_ID"] == system
            assert env["LANGFUSE_BASE_URL"] == f"http://{collector}:3000"
            assert env["LANGFUSE_PUBLIC_KEY"] == initialized["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
            assert env["LANGFUSE_SECRET_KEY"] == initialized["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
            assert not any(key.endswith("_LANGFUSE_SECRET_KEY") for key in env)
    assert len(public_keys) == len(secret_keys) == len(project_ids) == 3
    assert (
        services["langfuse-travel"]["depends_on"]["langfuse-web"]["condition"] == "service_healthy"
    )
    assert (
        services["langfuse-memory"]["depends_on"]["langfuse-travel"]["condition"]
        == "service_healthy"
    )


def test_gateways_allow_only_documented_application_interfaces(services):
    for gateway, target in [("travel-api", "travel-agent"), ("memory-api", "memory-service")]:
        assert services[gateway]["depends_on"][target]["condition"] == "service_healthy"
        assert str(services[gateway]["sysctls"]["net.ipv4.ip_forward"]) == "0"
        config = (ROOT / "infra" / "boundaries" / f"{gateway}.conf").read_text()
        assert "location / { return 404; }" in config
        assert 'proxy_set_header traceparent "";' in config
        assert 'proxy_set_header baggage "";' in config
    travel = (ROOT / "infra/boundaries/travel-api.conf").read_text()
    assert "location = /a2a/v1/message:send" in travel
    assert "location /mcp" not in travel
    memory = (ROOT / "infra/boundaries/memory-api.conf").read_text()
    assert "memory-orchestrator" not in memory


def test_images_include_only_their_own_service_code(services):
    for service in services.values():
        if "build" not in service:
            continue
        args = service["build"]["args"]
        assert args["SERVICE_PACKAGE"] == args["APP_MODULE"].split(".")[1]
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY services/common ./services/common" in dockerfile
    assert "COPY services/${SERVICE_PACKAGE} ./services/${SERVICE_PACKAGE}" in dockerfile
    assert "COPY services ./services" not in dockerfile


def test_travel_search_has_a_distinct_jwt_audience():
    realm = json.loads((ROOT / "infra/keycloak/agentic-banking-realm.json").read_text())
    clients = {client["clientId"]: client for client in realm["clients"]}

    def audiences(name):
        return {
            mapper.get("config", {}).get("included.client.audience")
            for mapper in clients[name].get("protocolMappers", [])
        }

    assert "travel-duckduckgo-mcp" in clients
    assert "travel-duckduckgo-mcp" in audiences("travel-agent")
    assert "duckduckgo-mcp" not in audiences("travel-agent")
    assert "travel-duckduckgo-mcp" not in audiences("banking-agent")
