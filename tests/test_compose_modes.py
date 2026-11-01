from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
@pytest.mark.parametrize("abs_mode", ["secure", "vulnerable"])
@pytest.mark.parametrize("provider", ["tutu", "mock"])
@pytest.mark.parametrize("note", ["clean.txt", "provider-note.txt"])
def test_one_env_file_configures_all_modes_without_extra_launch_options(
    tmp_path,
    abs_mode,
    provider,
    note,
):
    template = (ROOT / ".env.example").read_text()
    overrides = {"ABS_MCP_MODE": abs_mode, "FLIGHTS_PROVIDER": provider, "FLIGHTS_MOCK_NOTE": note}
    lines = []
    names = {"COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES", "COMPOSE_PROJECT_NAME"}
    for line in template.splitlines():
        key, separator, value = line.partition("=")
        if separator and not line.startswith("#"):
            names.add(key)
            line = f"{key}={overrides.get(key, value)}"
        lines.append(line)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "config", "--format", "json"],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if key not in names},
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["banking-agent"]["environment"]["ABS_MCP_MODE"] == abs_mode
    assert services["travel-agent"]["environment"]["FLIGHTS_PROVIDER"] == provider
    assert services["flights-mcp"]["environment"]["FLIGHTS_MCP_NOTE_FILE"] == f"/data/{note}"
    assert all(not service.get("profiles") for service in services.values())
    assert services["travel-agent"]["depends_on"]["flights-mcp"]["condition"] == "service_healthy"
    assert (ROOT / "infra" / "flights-mcp" / note).is_file()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
@pytest.mark.parametrize("interval", ["0", "10"])
def test_env_file_passes_memory_interval_to_orchestrator(tmp_path, interval):
    env_file = tmp_path / ".env"
    template = (ROOT / ".env.example").read_text()
    env_file.write_text(template.replace(
        "MEMORY_ORCHESTRATION_INTERVAL_SECONDS=1800",
        f"MEMORY_ORCHESTRATION_INTERVAL_SECONDS={interval}",
    ))
    excluded = {
        "MEMORY_ORCHESTRATION_INTERVAL_SECONDS", "COMPOSE_FILE", "COMPOSE_PROFILES",
        "COMPOSE_ENV_FILES", "COMPOSE_PROJECT_NAME",
    }
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "config", "--format", "json"],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if key not in excluded},
        check=True, capture_output=True, text=True,
    )
    environment = json.loads(result.stdout)["services"]["memory-orchestrator"]["environment"]
    assert environment["MEMORY_ORCHESTRATION_INTERVAL_SECONDS"] == interval
