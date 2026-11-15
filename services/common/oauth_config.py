"""Declarative OAuth client ceilings, shared by realm import and reconciliation."""

from services.common.banking_policy import TOOL_SCOPES
from services.common.skill_contract import SKILLS

SCOPE_AUDIENCES = {
    **{scope: ["bank-mcp", "abs-api"] for scope in sorted(set(TOOL_SCOPES.values()))},
    "travel.plan": ["travel-agent"],
    "memory.access": ["memory-service"],
}
CLIENT_SCOPES = {
    "bank-mcp": sorted(set(TOOL_SCOPES.values())),
    **{identity: [*scope.split(), "memory.access"] for identity, scope in SKILLS.values()},
}
TOKEN_CLIENTS = [
    "bank-ui",
    "banking-agent",
    "travel-agent",
    "memory-operator",
    "bank-mcp-vulnerable",
    *CLIENT_SCOPES,
]


def subject_mapper():
    return {
        "name": "subject",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-sub-mapper",
        "consentRequired": False,
        "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
    }


def audience_mapper(audience):
    return {
        "name": f"audience-{audience}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": False,
        "config": {"included.client.audience": audience, "access.token.claim": "true"},
    }


def scope_representation(name, audiences):
    return {
        "name": name,
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        "protocolMappers": [audience_mapper(aud) for aud in audiences],
    }


def customer_mapper():
    return {
        "name": "customer-id",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "consentRequired": False,
        "config": {
            "user.attribute": "customer_id",
            "claim.name": "customer_id",
            "jsonType.label": "String",
            "access.token.claim": "true",
        },
    }
