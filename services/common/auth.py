from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, status
from jwt.algorithms import RSAAlgorithm

from services.common.tracing import observation


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    username: str
    customer_id: str | None
    authorized_party: str
    claims: dict[str, Any]
    access_token: str = field(default="", repr=False, compare=False)


class JwksVerifier:
    """Small async JWKS verifier with an internal/external Keycloak URL split."""

    def __init__(self) -> None:
        self.issuer = os.getenv("KEYCLOAK_ISSUER", "http://localhost:8080/realms/agentic-banking")
        self.jwks_url = os.getenv(
            "KEYCLOAK_JWKS_URL",
            "http://keycloak:8080/realms/agentic-banking/protocol/openid-connect/certs",
        )
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with self._lock:
            if self._keys and time.monotonic() < self._expires_at:
                return
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
            self._keys = {
                item["kid"]: RSAAlgorithm.from_jwk(json.dumps(item))
                for item in response.json().get("keys", [])
                if item.get("kid")
            }
            self._expires_at = time.monotonic() + 300

    async def verify(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise jwt.InvalidTokenError("missing kid")
        if kid not in self._keys or time.monotonic() >= self._expires_at:
            await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            self._expires_at = 0
            await self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError("unknown signing key")
        return jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=self.issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "sub", "iss"]},
        )


_verifier = JwksVerifier()


async def require_principal(
    authorization: str | None = Header(default=None),
    x_debug_user: str | None = Header(default=None),
) -> Principal:
    """Authenticate a Keycloak token and enforce the caller allow-list for the service."""
    if os.getenv("AUTH_DISABLED", "false").lower() == "true":
        customer_id = x_debug_user or "customer-001"
        return Principal(customer_id, "debug-user", customer_id, "debug", {}, "debug-token")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = await _verifier.verify(token)
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or the identity provider is unavailable",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    azp = str(claims.get("azp", ""))
    allowed = {value.strip() for value in os.getenv("ALLOWED_AZP", "").split(",") if value.strip()}
    expected_audiences = {
        value.strip() for value in os.getenv("EXPECTED_AUDIENCE", "").split(",") if value.strip()
    }
    raw_audience = claims.get("aud", [])
    token_audiences = {raw_audience} if isinstance(raw_audience, str) else set(raw_audience)
    client_allowed = not allowed or azp in allowed
    audience_allowed = not expected_audiences or bool(expected_audiences & token_audiences)
    with observation(
        "oauth.authorize",
        user_id=claims.get("customer_id"),
        input={
            "azp": azp,
            "aud": raw_audience,
            "scope": claims.get("scope"),
            "expected_audience": sorted(expected_audiences),
        },
    ) as span:
        span.update(output={"client_allowed": client_allowed, "audience_allowed": audience_allowed})
    if not client_allowed:
        raise HTTPException(status_code=403, detail="Calling client is not allowed")
    if expected_audiences and not expected_audiences.intersection(token_audiences):
        raise HTTPException(status_code=403, detail="Token audience is not valid for this service")

    return Principal(
        subject=str(claims["sub"]),
        username=str(claims.get("preferred_username", claims["sub"])),
        customer_id=claims.get("customer_id"),
        authorized_party=azp,
        claims=claims,
        access_token=token,
    )


class ServiceTokenProvider:
    """Caches a Keycloak client-credentials token until shortly before expiry."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
    ) -> None:
        self.scope = scope
        self.client_id = client_id or os.environ["SERVICE_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SERVICE_CLIENT_SECRET"]
        base = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
        self.token_url = f"{base}/realms/agentic-banking/protocol/openid-connect/token"
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        **({"scope": self.scope} if self.scope else {}),
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
                response.raise_for_status()
            payload = response.json()
            self._token = payload["access_token"]
            self._expires_at = time.monotonic() + max(int(payload.get("expires_in", 60)) - 15, 5)
            return self._token


class TokenExchangeProvider:
    """Exchange an end-user token for a narrowly-audienced downstream token.

    Tokens are deliberately not cached: a cache keyed incorrectly here would become
    a cross-user identity vulnerability. Keycloak access-token exchange is cheap
    enough for this laboratory and keeps the security property obvious.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        audience: str = "abs-api",
        scope: str | None = None,
    ) -> None:
        self.client_id = client_id or os.environ["SERVICE_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SERVICE_CLIENT_SECRET"]
        self.audience = audience
        self.scope = scope
        base = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
        self.token_url = f"{base}/realms/agentic-banking/protocol/openid-connect/token"

    async def exchange(self, subject_token: str, *, scope: str | None = None) -> str:
        requested_scope = self.scope if scope is None else scope
        if not subject_token:
            raise ValueError("The authenticated end-user token is required for token exchange")
        with observation(
            "oauth.token-exchange",
            input={
                "client_id": self.client_id,
                "audience": self.audience,
                "scope": requested_scope,
            },
        ) as span:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "subject_token": subject_token,
                        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "audience": self.audience,
                        **({"scope": requested_scope} if requested_scope else {}),
                    },
                )
            body = response.json()
            granted = not response.is_error and (
                not requested_scope
                or set(requested_scope.split()) <= set(body.get("scope", "").split())
            )
            span.update(
                output={
                    "http_status": response.status_code,
                    "scope": body.get("scope"),
                    "expires_in": body.get("expires_in"),
                    "granted": granted,
                }
            )
            if not granted:
                raise ValueError("Keycloak token exchange rejected the requested authority")
            return str(body["access_token"])


def require_scope(principal: Principal, scope: str) -> None:
    """Only signed claims supplied by require_principal are authorization evidence."""
    granted = scope in str(principal.claims.get("scope", "")).split()
    with observation(
        "oauth.scope-check",
        input={
            "required_scope": scope,
            "azp": principal.authorized_party,
            "aud": principal.claims.get("aud"),
            "scope": principal.claims.get("scope"),
        },
        user_id=principal.customer_id,
    ) as span:
        span.update(output={"allowed": granted})
    if not granted:
        raise HTTPException(403, f"Required scope: {scope}")
