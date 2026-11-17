"""Verify agent turn trees in Banking and Travel Langfuse projects."""

import argparse
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx


def check_tree(rows, expected_root):
    roots = [row for row in rows if not row.get("parentObservationId")]
    if len(roots) != 1 or roots[0]["name"] != expected_root:
        raise ValueError(f"Expected one {expected_root} root, got {[r['name'] for r in roots]}")
    ids = {row["id"] for row in rows}
    if any(row.get("parentObservationId") not in ids for row in rows if row not in roots):
        raise ValueError("Missing parent observations")
    if sum(bool(row.get("isRootObservation")) for row in rows) != 1:
        raise ValueError("Langfuse reports multiple logical roots")
    return roots[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-trace-id", help="inspect this bank request in the local project"
    )
    args = parser.parse_args()
    system = os.environ["AGENT_SYSTEM_ID"]
    if system not in {"banking", "travel"}:
        raise ValueError(
            "This check covers agent turns in Banking and Travel; memory jobs have separate traces"
        )
    session = "trace-smoke-" + str(uuid.uuid4())
    started = datetime.now(UTC) - timedelta(minutes=15)
    collector = os.environ["LANGFUSE_BASE_URL"]
    auth = httpx.BasicAuth(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    with httpx.Client(timeout=180) as client:
        if not args.upstream_trace_id:
            assert system == "banking", "Start the request in banking-agent"
            keycloak = os.environ["KEYCLOAK_INTERNAL_URL"]
            login = client.post(
                keycloak + "/realms/agentic-banking/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "bank-ui",
                    "username": "ivan.petrov",
                    "password": "banking-demo",
                },
            )
            login.raise_for_status()
            response = client.post(
                "http://127.0.0.1:8000/v1/chat",
                headers={"Authorization": "Bearer " + login.json()["access_token"]},
                json={
                    "message": "Что посмотреть в Стамбуле?",
                    "conversation_id": session,
                    "travel": {"request_type": "destination_info", "destination": "Стамбул"},
                    "experiment": {"experiment_id": "trace-smoke", "run_id": session},
                },
            )
            response.raise_for_status()
            assert response.json()["intent"] == "travel"
        params = {
            "fields": "core,basic,trace_context,metadata",
            "limit": 1000,
            "fromStartTime": started.isoformat(),
            "toStartTime": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

        def observations(**filters):
            rows, cursor = [], None
            while True:
                query = client.get(
                    collector + "/api/public/v2/observations",
                    auth=auth,
                    params={**params, **filters, **({"cursor": cursor} if cursor else {})},
                )
                query.raise_for_status()
                page = query.json()
                rows.extend(page["data"])
                cursor = page.get("meta", {}).get("cursor")
                if not cursor:
                    return rows

        if args.upstream_trace_id:
            filters = {
                "filter": json.dumps(
                    [
                        {
                            "type": "stringObject",
                            "column": "metadata",
                            "key": "upstream_trace_id",
                            "operator": "=",
                            "value": args.upstream_trace_id,
                        }
                    ]
                )
            }
        else:
            filters = {"sessionId": session}
        required = {
            "banking": {
                "banking-agent.turn",
                "oauth.authorize",
                "skill.turn",
                "a2a.travel.call",
                "memory.skill.http.context",
                "orchestrator.compose",
                "llm.banking-agent.orchestrator_response",
            },
            "travel": {"a2a.travel.send-message", "mcp.tool.call", "oauth.authorize"},
        }[system]
        deadline = time.monotonic() + 120
        last_error = "No observations delivered"
        while time.monotonic() < deadline:
            trace_ids = {row["traceId"] for row in observations(**filters)}
            if system == "banking" and len(trace_ids) > 1:
                raise ValueError(f"Bank request fragmented into {len(trace_ids)} traces")
            reports, names = [], set()
            try:
                for trace_id in sorted(trace_ids):
                    rows = observations(traceId=trace_id)
                    root = check_tree(rows, f"{system}-agent.request")
                    assert all(
                        row["projectId"] == os.environ["LANGFUSE_PROJECT_ID"] for row in rows
                    )
                    assert all(
                        (row.get("metadata") or {}).get("system_id") == system for row in rows
                    )
                    names.update(row["name"] for row in rows)
                    reports.append(
                        {
                            "trace_id": trace_id,
                            "root": root["name"],
                            "observations": len(rows),
                            "physical_roots": 1,
                            "logical_roots": 1,
                            "upstream_trace_id": (root.get("metadata") or {}).get(
                                "upstream_trace_id"
                            ),
                            "url": f"http://localhost:3001/project/{root['projectId']}/traces/{trace_id}",
                        }
                    )
                if missing := required - names:
                    raise ValueError(f"Missing observations: {sorted(missing)}")
            except ValueError as exc:
                last_error = str(exc)
            else:
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "system": system,
                            "project_id": os.environ["LANGFUSE_PROJECT_ID"],
                            "traces": reports,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            time.sleep(3)
        raise RuntimeError(last_error)


if __name__ == "__main__":
    main()
