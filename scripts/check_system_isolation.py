"""Read-only checks of a running stand: network boundaries, API gateways and project keys."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--env-file", ".env"]
PORTS = {
    "banking-agent": 8000,
    "travel-agent": 8003,
    "memory-service": 8002,
    "memory-orchestrator": 8004,
    "abs-api": 8001,
    "flights-mcp": 8106,
    "mongo": 27017,
    "postgres": 5432,
}
DENIED = {
    "banking-agent": [
        "travel-agent",
        "flights-mcp",
        "memory-service",
        "memory-orchestrator",
        "mongo",
        "postgres",
    ],
    "travel-agent": [
        "banking-agent",
        "abs-api",
        "memory-service",
        "memory-orchestrator",
        "mongo",
        "postgres",
    ],
    "memory-orchestrator": ["banking-agent", "abs-api", "travel-agent", "flights-mcp", "postgres"],
}
ALLOWED = {
    "banking-agent": [
        "http://travel-api:8003/health",
        "http://memory-api:8002/health",
        "http://abs-mcp:8101/health",
    ],
    "travel-agent": [
        "http://travel-agent:8003/health",
        "http://flights-mcp:8106/health",
        "http://travel-duckduckgo-mcp:8105/health",
    ],
    "memory-orchestrator": [
        "http://memory-orchestrator:8004/health",
        "http://memory-service:8002/health",
    ],
}
PROBE = r"""
import base64, importlib.util, json, os, socket, sys, urllib.error, urllib.request
checks = json.loads(sys.argv[1])
results = []
for label, address, port in checks['denied']:
    try:
        with socket.create_connection((address, port), timeout=0.5):
            passed = False
    except OSError:
        passed = True
    results.append((label, passed))
for url in checks['allowed']:
    with urllib.request.urlopen(url, timeout=5) as response:
        results.append((url, response.status == 200))
for url in checks['blocked_routes']:
    try:
        urllib.request.urlopen(url, timeout=5)
        results.append((url, False))
    except urllib.error.HTTPError as error:
        results.append((url, error.code == 404))
authorization = base64.b64encode((os.environ['LANGFUSE_PUBLIC_KEY'] + ':' +
                                 os.environ['LANGFUSE_SECRET_KEY']).encode()).decode()
request = urllib.request.Request(os.environ['LANGFUSE_BASE_URL'] + '/api/public/projects',
                                 headers={'Authorization': 'Basic ' + authorization})
with urllib.request.urlopen(request, timeout=10) as response:
    projects = json.load(response)['data']
project_ids = [project['id'] for project in projects]
results.append(('Langfuse project key scope', project_ids == [os.environ['LANGFUSE_PROJECT_ID']]))
packages = {'banking': 'banking_agent', 'travel': 'travel_agent', 'memory': 'memory_service'}
for system, package in packages.items():
    present = importlib.util.find_spec('services.' + package) is not None
    expected = system == os.environ['AGENT_SYSTEM_ID']
    results.append(('Code package ' + package, present == expected))
if os.environ['AGENT_SYSTEM_ID'] == 'banking':
    import asyncio, uuid
    from datetime import date, timedelta
    from services.banking_agent.clients import MemoryClient, TravelA2AClient
    from services.common.auth import ServiceTokenProvider
    async def interfaces():
        tokens = ServiceTokenProvider()
        correlation = 'isolation-' + str(uuid.uuid4())
        source = await TravelA2AClient(tokens).mock_flight_source({
            'origin': 'MOW', 'destination': 'LED',
            'departure_date': (date.today() + timedelta(days=30)).isoformat(),
        }, correlation)
        history = await MemoryClient(tokens).conversation_messages('customer-001', correlation)
        return bool(source['results'][0]['synthetic']), history == []
    a2a, memory = asyncio.run(interfaces())
    results.extend([('Authenticated A2A mock source', a2a), ('Authenticated memory API', memory)])
print(json.dumps(results))
"""


def run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def main() -> int:
    addresses = {}
    for service in PORTS:
        identifier = run([*COMPOSE, "ps", "-q", service])
        if not identifier:
            raise RuntimeError(f"Service is not running: {service}")
        networks = json.loads(
            run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", identifier])
        )
        addresses[service] = [network["IPAddress"] for network in networks.values()]
    failed = []
    for caller, denied in DENIED.items():
        checks = {
            "denied": [
                (service, address, PORTS[service])
                for service in denied
                for address in addresses[service]
            ],
            "allowed": ALLOWED[caller],
            "blocked_routes": (
                [
                    "http://travel-api:8003/mcp",
                    "http://travel-api:8003/v1/bookings/mock",
                    "http://memory-api:8002/api/v1/orchestrator/collect",
                ]
                if caller == "banking-agent"
                else []
            ),
        }
        results = json.loads(
            run([*COMPOSE, "exec", "-T", caller, "python", "-c", PROBE, json.dumps(checks)])
        )
        failures = [name for name, passed in results if not passed]
        failed.extend(f"{caller}: {name}" for name in failures)
        print(f"{caller}: {len(results) - len(failures)}/{len(results)} checks passed")
    if failed:
        raise RuntimeError("Isolation checks failed: " + "; ".join(failed))
    print("System isolation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
