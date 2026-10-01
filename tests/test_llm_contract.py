from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from services.common.llm import (
    LLMClient,
    LLMServiceError,
    install_llm_error_handlers,
    raise_upstream_llm_error,
)


class Result(BaseModel):
    value: str


class FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        content = "not-json" if len(self.requests) == 1 else '{"value":"repaired"}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
        )


@pytest.mark.asyncio
async def test_llm_is_required_and_custom_base_url_is_used(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.internal:11434/v1")
    monkeypatch.setenv("OPENAI_MODEL", "local-model")
    llm = LLMClient("test-agent")
    assert str(llm.client.base_url) == "http://llm.internal:11434/v1/"
    with pytest.raises(LLMServiceError, match="OPENAI_API_KEY") as captured:
        await llm.generate_json(
            Result,
            instructions="Return a result",
            payload={"input": "test"},
            operation="test",
        )
    assert captured.value.status_code == 503


@pytest.mark.asyncio
async def test_invalid_structured_output_gets_one_llm_repair_attempt(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = LLMClient("test-agent")
    completions = FakeCompletions()
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await llm.generate_json(
        Result,
        instructions="Return a result",
        payload={"input": "test"},
        operation="test",
    )

    assert result.value == "repaired"
    assert len(completions.requests) == 2
    assert "не прошёл JSON Schema validation" in completions.requests[1]["messages"][-1]["content"]


def test_llm_error_is_user_visible_http_response() -> None:
    app = FastAPI()
    install_llm_error_handlers(app)

    @app.get("/fail")
    async def fail() -> None:
        raise LLMServiceError("ИИ-модель недоступна: тест")

    with TestClient(app) as client:
        response = client.get("/fail")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "ИИ-модель недоступна: тест",
        "code": "llm_unavailable",
    }


def test_upstream_llm_error_keeps_public_message() -> None:
    response = httpx.Response(
        503,
        json={"detail": "ИИ-модель недоступна: провайдер", "code": "llm_unavailable"},
    )
    with pytest.raises(LLMServiceError, match="провайдер"):
        raise_upstream_llm_error(response)
