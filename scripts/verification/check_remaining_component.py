"""Isolated component probes: local signing key and simulated provider responses.

No running server configuration, stored data or real issuer keys are changed.
"""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from services.banking_agent.graph import BankingGraph
from services.common import auth
from services.common.llm import LLMClient, LLMServiceError


async def main():
    out = {
        "scope": "Isolated components; generated RSA trust anchor, synthetic signed claims and mocked completion transport. Not live Keycloak or real LLM behavior.",  # noqa: E501 -- preserve original probe text
        "auth": [],
        "answers": [],
    }
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = auth.JwksVerifier()
    verifier._keys = {"verification-only": key.public_key()}
    verifier._expires_at = time.monotonic() + 300
    now = int(time.time())
    base = {
        "sub": "synthetic-user",
        "iss": verifier.issuer,
        "iat": now,
        "exp": now + 300,
        "azp": "bank-ui",
        "aud": ["banking-agent"],
        "customer_id": "customer-001",
    }
    config = {
        "AUTH_DISABLED": "false",
        "ALLOWED_AZP": "bank-ui",
        "EXPECTED_AUDIENCE": "banking-agent",
    }
    cases = [
        ("valid", {}, {}, 200),
        ("wrong-issuer", {"iss": "https://wrong-issuer.invalid"}, {}, 401),
        ("expired", {"exp": now - 60}, {}, 401),
        ("wrong-audience", {"aud": ["other-service"]}, {}, 403),
        ("wrong-client", {"azp": "other-client"}, {}, 403),
        (
            "empty-allow-lists",
            {"azp": "other-client", "aud": ["other-service"]},
            {"ALLOWED_AZP": "", "EXPECTED_AUDIENCE": ""},
            200,
        ),
        ("auth-disabled", {}, {"AUTH_DISABLED": "true"}, 200),
        ("wrong-signature", {}, {}, 401),
    ]
    for label, changed, env, expected in cases:
        signing_key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if label == "wrong-signature"
            else key
        )
        token = jwt.encode(
            {**base, **changed},
            signing_key,
            algorithm="RS256",
            headers={"kid": "verification-only"},
        )
        with patch.object(auth, "_verifier", verifier), patch.dict(os.environ, {**config, **env}):
            try:
                principal = await auth.require_principal(
                    authorization=None if label == "auth-disabled" else "Bearer " + token,
                    x_debug_user="customer-002" if label == "auth-disabled" else None,
                )
                status, detail = (
                    200,
                    {
                        "subject": principal.subject,
                        "customer_id": principal.customer_id,
                        "authorized_party": principal.authorized_party,
                    },
                )
            except HTTPException as exc:
                status, detail = exc.status_code, exc.detail
        out["auth"].append(
            {
                "case": label,
                "http_equivalent": status,
                "expected_component_behavior": expected,
                "matches_expected": status == expected,
                "detail": detail,
                "configuration": {**config, **env},
            }
        )

    # Real JSON validation and compose_response, only the provider transport is replaced.
    llm = LLMClient("verification-isolated")
    llm.api_key = "synthetic-not-sent"
    graph = BankingGraph(
        None,
        SimpleNamespace(
            **{
                name: noop
                for name in (
                    "profile",
                    "accounts",
                    "operations",
                    "investments",
                    "travel_plan",
                    "general",
                )
            }
        ),
        llm,
    )
    state = {
        "intent": "accounts",
        "message": "Какой у меня остаток?",
        "response": "Остаток 100 RUB.",
        "data": {"accounts": [{"id": "synthetic-account", "balance": "100.00", "currency": "RUB"}]},
    }

    async def completion(content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None
        )

    for label, raw in [
        ("valid-control", '{"message":"Остаток 100 RUB."}'),
        ("false-balance", '{"message":"Подтверждённый остаток 999999 RUB."}'),
        ("invalid-schema", '{"wrong_field":123}'),
    ]:

        async def respond(raw=raw, **kwargs):
            return await completion(raw)

        with patch.object(llm.client.chat.completions, "create", side_effect=respond) as mocked:
            try:
                result, error = await graph.compose_response(state), None
            except LLMServiceError as exc:
                result, error = None, {"code": exc.code, "http_status": exc.status_code}
        out["answers"].append(
            {
                "case": label,
                "trusted_state": state,
                "injected_completion": raw,
                "result": result,
                "error": error,
                "provider_calls": mocked.call_count,
            }
        )
    with patch.object(
        llm.client.chat.completions,
        "create",
        side_effect=AssertionError("Action branch must skip generation"),
    ) as mocked:
        canonical = await graph.compose_response(
            {
                **state,
                "intent": "action",
                "response": "Подготовлен перевод 1 RUB; требуется подтверждение.",
            }
        )
    out["action_control"] = {
        "patch": canonical,
        "provider_calls": mocked.call_count,
        "original_response_preserved": "response" not in canonical,
    }
    await llm.client.close()
    print("__EVIDENCE__" + json.dumps(out, ensure_ascii=False), flush=True)


async def noop(state):
    return {}


asyncio.run(main())
