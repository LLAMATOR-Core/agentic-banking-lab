"""Reproducible baseline checks for PDF plan P01-P14. Never records credentials."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT
ART = Path(__file__).resolve().parent
PROJECT = "abl-stage-verification"
KC = "http://127.0.0.1:8080/realms/agentic-banking/protocol/openid-connect/token"


def stamp():
    return datetime.now(UTC).isoformat()


def env_values():
    result = {}
    for line in (REPO / ".env").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def clean(value):
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if any(
                    s in k.lower()
                    for s in (
                        "password",
                        "secret",
                        "access_token",
                        "refresh_token",
                        "authorization",
                        "api_key",
                    )
                )
                else clean(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT REDACTED]", value)
    return value


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(clean(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


class Run:
    def __init__(self, folder=None):
        if folder:
            self.dir = Path(folder)
        else:
            self.dir = (
                REPO
                / "docs/verification/evidence"
                / ("run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
            )
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results = []
        self.tokens = {}
        self.n = 0
        resultfile = self.dir / "results.json"
        if resultfile.exists():
            self.results = json.loads(resultfile.read_text(encoding="utf-8"))
        self.config = env_values()

    def save(self, name, data):
        dump(self.dir / name, data)

    def record(
        self, test, status, description, evidence=None, scope="live HTTP / actual Docker services"
    ):
        item = {
            "test": test,
            "status": status,
            "description": description,
            "evidence": evidence or [],
            "scope": scope,
            "time_utc": stamp(),
        }
        self.results.append(item)
        self.save("results.json", self.results)
        print(f"{test}: {status} — {description}", flush=True)

    def request(self, label, method, url, token=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = "Bearer " + token
        start = time.perf_counter()
        evidence = {
            "time_utc": stamp(),
            "method": method,
            "url": url,
            "request": kwargs.get("json", kwargs.get("params")),
            "authenticated": bool(token),
        }
        try:
            r = httpx.request(
                method,
                url,
                headers=headers,
                trust_env=False,
                timeout=kwargs.pop("timeout", 35),
                **kwargs,
            )
            try:
                body = r.json()
            except ValueError:
                body = r.text[:30000]
            evidence.update(http_status=r.status_code, response=body)
        except Exception as exc:
            r = None
            body = None
            evidence.update(error=type(exc).__name__ + ": " + str(exc))
        evidence["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
        self.save("http/" + label + ".json", evidence)
        return evidence

    def login(self, who):
        if who in ("A", "B"):
            data = {
                "grant_type": "password",
                "client_id": "bank-ui",
                "username": {"A": "ivan.petrov", "B": "anna.sokolova"}[who],
                "password": "banking-demo",
            }
        else:
            secret_key = {
                "banking-agent": "KEYCLOAK_BANKING_AGENT_SECRET",
                "bank-mcp": "KEYCLOAK_BANK_MCP_SECRET",
                "memory-operator": "KEYCLOAK_MEMORY_OPERATOR_SECRET",
                "travel-agent": "KEYCLOAK_TRAVEL_AGENT_SECRET",
            }[who]
            data = {
                "grant_type": "client_credentials",
                "client_id": who,
                "client_secret": self.config.get(secret_key, who + "-secret"),
            }
        r = httpx.post(KC, data=data, trust_env=False, timeout=20)
        r.raise_for_status()
        token = r.json()["access_token"]
        self.tokens[who] = token
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        self.save(
            "identities/" + who + ".json",
            {k: claims.get(k) for k in ("sub", "azp", "aud", "customer_id", "exp", "iat", "iss")},
        )
        return token

    def mcp(self, label, who="A", port=8101, tool="get_customer_accounts", args=None):
        return self.request(
            label,
            "POST",
            f"http://127.0.0.1:{port}/mcp",
            self.tokens[who],
            json={
                "jsonrpc": "2.0",
                "id": label,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}},
            },
        )

    @staticmethod
    def value(e):
        b = e.get("response")
        if not isinstance(b, dict):
            return None
        r = b.get("result", {})
        return r.get("structuredContent") if not r.get("isError", False) else None

    def accounts(self, label, who="A"):
        e = self.mcp(label, who)
        rows = self.value(e)
        if not isinstance(rows, list):
            raise RuntimeError("Account positive control failed: " + label)
        return rows

    def balances(self, label):
        data = {w: self.accounts(label + "-" + w, w) for w in ("A", "B")}
        self.save("snapshots/" + label + ".json", data)
        return {a["id"]: Decimal(str(a["balance"])) for rows in data.values() for a in rows}

    def transfer(self, label, amount="1.00", key=None, target=None, who="A", port=8101, extra=None):
        args = {
            "from_account": self.a["id"],
            "to_account": target or self.b["number"],
            "amount": amount,
            "description": "PDF verification " + label,
            "idempotency_key": key or "pdf-" + uuid.uuid4().hex,
        }
        if extra:
            args.update(extra)
        return self.mcp(label, who, port, "create_transfer", args)

    def baseline(self):
        commit = subprocess.check_output(
            [
                "git",
                "-c",
                "safe.directory=" + str(REPO).replace("\\", "/"),
                "-C",
                str(REPO),
                "rev-parse",
                "HEAD",
            ],
            text=True,
        ).strip()
        pdf = ROOT / "docs/reports/СпиридоновМихаилАндреевич_2.pdf"
        self.save(
            "manifest.json",
            {
                "started_utc": stamp(),
                "timezone": "Europe/Moscow",
                "pdf": pdf.name,
                "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                "commit": commit,
                "docker_project": PROJECT,
                "config": {
                    k: v
                    for k, v in self.config.items()
                    if k
                    in [
                        "ABS_MCP_MODE",
                        "FLIGHTS_PROVIDER",
                        "FLIGHTS_MOCK_NOTE",
                        "AUTH_DISABLED",
                        "OPENAI_BASE_URL",
                        "OPENAI_MODEL",
                        "EMBEDDING_MODEL",
                        "MEMORY_ORCHESTRATION_INTERVAL_SECONDS",
                    ]
                },
                "model_key_configured": bool(self.config.get("OPENAI_API_KEY")),
                "important": "Baseline of the existing implementation; proposed grant/approval service is not implemented. PASS applies only to the observed subcase.",  # noqa: E501 -- preserve original probe text
            },
        )
        for port in (8000, 8001, 8002, 8003, 8004, 8101, 8104, 8105, 8106):
            self.request("health-" + str(port), "GET", f"http://127.0.0.1:{port}/health")
        self.login("A")
        self.login("B")
        a = self.accounts("P01-secure-A")
        b = self.accounts("P01-secure-B", "B")
        self.a = next(x for x in a if x["currency"] == "RUB")
        self.b = next(x for x in b if x["currency"] == "RUB")
        self.save("accounts-selected.json", {"A": self.a, "B": self.b})
        self.record(
            "P01.secure",
            "PASS" if {x["id"] for x in a}.isdisjoint({x["id"] for x in b}) else "FAIL",
            "Изоляция счетов A/B через защищённый MCP.",
            ["http/P01-secure-A.json", "http/P01-secure-B.json"],
        )
        e = self.mcp("P01-extra-customer-id", args={"customer_id": "customer-002"})
        self.record(
            "P01.extra-field",
            "PASS" if self.value(e) == a else "FAIL",
            "Лишний customer_id не меняет клиента защищённого MCP; отдельной проверки отклонения лишнего поля это не доказывает.",  # noqa: E501 -- preserve original probe text
            ["http/P01-extra-customer-id.json"],
        )
        e = self.request(
            "P01-lab-at-secure",
            "POST",
            "http://127.0.0.1:8000/v1/lab/vulnerable-abs",
            self.tokens["A"],
            json={"tool": "get_customer_accounts", "customer_id": "customer-002"},
        )
        leaked = isinstance(e.get("response"), dict) and e["response"].get("result") == b
        self.record(
            "P01.lab",
            "FAIL" if leaked else "INCONCLUSIVE",
            "Лабораторный маршрут при secure "
            + ("выдал A счета B." if leaked else "не дал ожидаемого сравнимого ответа."),
            ["http/P01-lab-at-secure.json"],
        )
        e = self.mcp("P01-vulnerable-direct", port=8104, args={"customer_id": "customer-002"})
        self.record(
            "P01.vulnerable",
            "FAIL" if self.value(e) == b else "INCONCLUSIVE",
            "Прямой уязвимый MCP: проверка выбора чужого клиента.",
            ["http/P01-vulnerable-direct.json"],
        )
        self.balances("initial")

        before = self.balances("P02-before")
        e = self.transfer("P02-direct-no-grant")
        after = self.balances("P02-after")
        delta = before[self.a["id"]] - after[self.a["id"]]
        self.record(
            "P02.no-grant",
            "FAIL"
            if delta == 1
            else ("PASS" if delta == 0 and self.value(e) is None else "INCONCLUSIVE"),
            f"Прямой денежный вызов без подтверждения: списано {delta} RUB.",
            [
                "http/P02-direct-no-grant.json",
                "snapshots/P02-before.json",
                "snapshots/P02-after.json",
            ],
        )
        self.record(
            "P02.valid-grant",
            "NOT_IMPLEMENTED",
            "Предлагаемый сервис одноразовых разрешений отсутствует; положительный запрос с настоящим grant выполнить нельзя.",  # noqa: E501 -- preserve original probe text
        )
        e = self.transfer("P02-foreign-source", extra={"from_account": self.b["id"]})
        self.record(
            "P02.owner-control",
            "PASS" if self.value(e) is None else "FAIL",
            "Защищённый MCP: клиент A пытается списать средства со счёта B.",
            ["http/P02-foreign-source.json"],
        )
        e = self.transfer(
            "P03-grant-field",
            extra={"grant": {"owner": "customer-002", "expires_at": "2000-01-01"}},
        )
        self.record(
            "P03",
            "NOT_IMPLEMENTED",
            "Поля и механизм grant отсутствуют. Отказ из-за лишнего поля не является проверкой владельца, срока или подписи разрешения.",  # noqa: E501 -- preserve original probe text
            ["http/P03-grant-field.json"],
        )

        key = "pdf-seq-" + uuid.uuid4().hex
        before = self.balances("P04-seq-before")
        e1 = self.transfer("P04-seq-first", key=key)
        e2 = self.transfer("P04-seq-repeat", key=key)
        after = self.balances("P04-seq-after")
        delta = before[self.a["id"]] - after[self.a["id"]]
        self.record(
            "P04.sequential",
            "PASS" if delta == 1 and self.value(e1) == self.value(e2) else "FAIL",
            f"Два одинаковых последовательных запроса: списано {delta} RUB.",
            [
                "http/P04-seq-first.json",
                "http/P04-seq-repeat.json",
                "snapshots/P04-seq-before.json",
                "snapshots/P04-seq-after.json",
            ],
        )
        e3 = self.transfer("P04-key-conflict", amount="2.00", key=key)
        self.record(
            "P04.changed-payload",
            "FAIL" if self.value(e3) == self.value(e1) and self.value(e3) is not None else "PASS",
            "Повтор ключа с другой суммой: "
            + (
                "возвращён старый результат вместо конфликта."
                if self.value(e3) == self.value(e1)
                else "ответ отличается от прежнего результата; см. артефакт."
            ),
            ["http/P04-key-conflict.json"],
        )
        key = "pdf-parallel-" + uuid.uuid4().hex
        before = self.balances("P04-parallel-before")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(lambda i: self.transfer("P04-parallel-" + str(i), key=key), range(4))
            )
        after = self.balances("P04-parallel-after")
        delta = before[self.a["id"]] - after[self.a["id"]]
        successes = [self.value(r) for r in responses]
        stable = all(x is not None and x == successes[0] for x in successes)
        self.record(
            "P04.parallel-effect",
            "PASS" if delta == 1 else "FAIL",
            f"Четыре параллельных запроса одного ключа: списано {delta} RUB. Один опыт не доказывает отсутствие всех гонок.",  # noqa: E501 -- preserve original probe text
            ["snapshots/P04-parallel-before.json", "snapshots/P04-parallel-after.json"]
            + ["http/P04-parallel-" + str(i) + ".json" for i in range(4)],
        )
        self.record(
            "P04.parallel-responses",
            "PASS" if stable else "FAIL",
            "Проверка одинакового успешного результата для всех параллельных повторов.",
        )

        for label, target in [
            ("number", self.b["number"]),
            ("internal-id", self.b["id"]),
            ("unknown", "pdf-no-such-account"),
            ("external-number", "40817810000099999999"),
        ]:
            before = self.balances("P05-" + label + "-before")
            e = self.transfer("P05-" + label, target=target)
            after = self.balances("P05-" + label + "-after")
            debit = before[self.a["id"]] - after[self.a["id"]]
            credit = after[self.b["id"]] - before[self.b["id"]]
            status = (
                ("PASS" if debit == 1 and credit == 1 else "FAIL")
                if label == "number"
                else ("FAIL" if debit > 0 else "PASS")
            )
            if label == "external-number":
                status = "PARTIAL"
            self.record(
                "P05." + label,
                status,
                f"Получатель {label}: списание {debit}, зачисление на тестовый счёт B {credit} RUB. Явного признака внутренний/внешний получатель в API нет.",  # noqa: E501 -- preserve original probe text
                [
                    "http/P05-" + label + ".json",
                    "snapshots/P05-" + label + "-before.json",
                    "snapshots/P05-" + label + "-after.json",
                ],
            )

        for who in ("banking-agent", "bank-mcp", "memory-operator"):
            self.login(who)
        cases = [
            ("missing", "GET", "http://127.0.0.1:8000/v1/dashboard", None, None, 401),
            ("forged", "GET", "http://127.0.0.1:8000/v1/dashboard", "not.a.valid-token", None, 401),
            (
                "user-to-memory",
                "GET",
                "http://127.0.0.1:8002/api/v1/working/conversations/banking-agent/customer-001",
                self.tokens["A"],
                None,
                403,
            ),
            (
                "bank-mcp-to-memory",
                "GET",
                "http://127.0.0.1:8002/api/v1/working/conversations/banking-agent/customer-001",
                self.tokens["bank-mcp"],
                None,
                403,
            ),
            (
                "bank-to-operator",
                "POST",
                "http://127.0.0.1:8004/api/v1/orchestrator/collect",
                self.tokens["banking-agent"],
                {"limit": 1},
                403,
            ),
            (
                "flights-no-token",
                "POST",
                "http://127.0.0.1:8106/mcp",
                None,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                401,
            ),
            (
                "user-to-travel",
                "POST",
                "http://127.0.0.1:8003/a2a/v1/message:send",
                self.tokens["A"],
                {},
                403,
            ),
        ]
        for label, method, url, token, data, expected in cases:
            e = self.request(
                "P10-" + label, method, url, token, **({"json": data} if data is not None else {})
            )
            self.record(
                "P10." + label,
                "PASS" if e.get("http_status") == expected else "FAIL",
                f"Ожидался HTTP {expected}; получен {e.get('http_status')}.",
                ["http/P10-" + label + ".json"],
            )
        e = self.request(
            "P10-memory-positive",
            "GET",
            "http://127.0.0.1:8002/api/v1/working/conversations/banking-agent/customer-001",
            self.tokens["banking-agent"],
        )
        self.record(
            "P10.memory-positive",
            "PASS" if e.get("http_status") == 200 else "FAIL",
            "Положительный контроль памяти с разрешённым сервисным токеном.",
            ["http/P10-memory-positive.json"],
        )
        e = self.request(
            "P14-chat-model-state",
            "POST",
            "http://127.0.0.1:8000/v1/chat",
            self.tokens["A"],
            json={"message": "Покажи мои счета", "conversation_id": "pdf-" + uuid.uuid4().hex},
            timeout=90,
        )
        self.record(
            "P14.model-unavailable",
            "PASS" if e.get("http_status") == 503 else "PARTIAL",
            "Проверка явной ошибки при недоступной модели; ложный успех не должен возвращаться.",
            ["http/P14-chat-model-state.json"],
        )
        self.balances("baseline-final")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    run = Run(args.run)
    print("Artifacts: " + str(run.dir), flush=True)
    try:
        if args.baseline:
            run.baseline()
    except Exception as exc:
        run.record("RUN.ERROR", "ERROR", type(exc).__name__ + ": " + str(exc))
        raise


if __name__ == "__main__":
    main()
