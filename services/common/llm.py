from __future__ import annotations

import json
import logging
import os
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from services.common.tracing import observation

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMServiceError(RuntimeError):
    """User-safe error raised when an LLM-backed agent cannot complete a step."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "llm_unavailable",
        status_code: int = 503,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LLMResponseError(LLMServiceError):
    def __init__(self, message: str = "ИИ-модель вернула некорректный структурированный ответ"):
        super().__init__(message, code="llm_invalid_response", status_code=502)


class LLMClient:
    """Required OpenAI-compatible LLM client used by every agent in the stand."""

    def __init__(self, component: str) -> None:
        self.component = component
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()
        self.json_mode = os.getenv("OPENAI_JSON_MODE", "true").lower() == "true"
        try:
            timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        except ValueError:
            timeout = 45.0
        self.client = AsyncOpenAI(
            api_key=self.api_key or "not-configured",
            base_url=self.base_url or "https://api.openai.com/v1",
            timeout=timeout,
            max_retries=0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def public_config(self) -> dict[str, Any]:
        parsed = urlsplit(self.base_url)
        provider = parsed.netloc or parsed.path or "not-configured"
        return {
            "required": True,
            "configured": self.configured,
            "model": self.model or "not-configured",
            "provider": provider,
        }

    async def generate_json(
        self,
        schema: type[SchemaT],
        *,
        instructions: str,
        payload: Any,
        operation: str,
    ) -> SchemaT:
        if not self.configured:
            raise LLMServiceError(
                "ИИ-модель недоступна: задайте OPENAI_API_KEY, OPENAI_BASE_URL и OPENAI_MODEL"
            )

        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system_message = (
            f"{instructions}\n\n"
            "Верни только один JSON-объект без Markdown. "
            f"Он обязан соответствовать JSON Schema: {schema_json}"
        )
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_message},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
        }
        if self.json_mode:
            request["response_format"] = {"type": "json_object"}

        with observation(
            f"llm.{self.component}.{operation}",
            input={"schema": schema.__name__},
            metadata={"component": self.component, "model": self.model},
        ) as span:
            for format_attempt in range(2):
                try:
                    response = await self.client.chat.completions.create(**request)
                except (APIConnectionError, APITimeoutError) as exc:
                    logger.warning("LLM connection failed for %s: %s", self.component, exc)
                    raise LLMServiceError(
                        "ИИ-модель недоступна: не удалось подключиться к провайдеру"
                    ) from exc
                except AuthenticationError as exc:
                    logger.warning("LLM authentication failed for %s", self.component)
                    raise LLMServiceError(
                        "ИИ-модель недоступна: провайдер отклонил API-ключ"
                    ) from exc
                except RateLimitError as exc:
                    logger.warning("LLM rate limit for %s", self.component)
                    raise LLMServiceError(
                        "ИИ-модель временно недоступна: превышен лимит провайдера"
                    ) from exc
                except APIStatusError as exc:
                    logger.warning(
                        "LLM provider returned HTTP %s for %s", exc.status_code, self.component
                    )
                    raise LLMServiceError(
                        f"ИИ-модель недоступна: провайдер вернул HTTP {exc.status_code}"
                    ) from exc
                except OpenAIError as exc:
                    logger.warning(
                        "OpenAI SDK error for %s: %s", self.component, type(exc).__name__
                    )
                    raise LLMServiceError("ИИ-модель недоступна: ошибка провайдера") from exc

                if not response.choices or not response.choices[0].message.content:
                    raise LLMResponseError("ИИ-модель не вернула ответ")
                raw_content = response.choices[0].message.content.strip()
                content = raw_content
                if content.startswith("```"):
                    lines = content.splitlines()
                    content = "\n".join(lines[1:-1]).strip()
                try:
                    parsed = schema.model_validate_json(content)
                except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                    validation_summary = _validation_error_summary(exc)
                    logger.warning(
                        "Invalid LLM JSON for %s/%s (attempt %s): %s",
                        self.component,
                        operation,
                        format_attempt + 1,
                        validation_summary,
                    )
                    if format_attempt == 0:
                        request["messages"] = [
                            *request["messages"],
                            {"role": "assistant", "content": raw_content[:12000]},
                            {
                                "role": "user",
                                "content": (
                                    "Предыдущий ответ не прошёл JSON Schema validation. "
                                    f"Ошибки: {validation_summary}. Исправь ответ и верни "
                                    "только валидный JSON-объект."
                                ),
                            },
                        ]
                        continue
                    raise LLMResponseError() from exc
                usage = getattr(response, "usage", None)
                span.update(
                    output={"schema": schema.__name__},
                    metadata={
                        "component": self.component,
                        "model": self.model,
                        "format_retries": format_attempt,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                    },
                )
                return parsed

        raise LLMResponseError()


def install_llm_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LLMServiceError)
    async def llm_error_handler(_: Request, exc: LLMServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "code": exc.code},
        )


def raise_upstream_llm_error(response: httpx.Response) -> None:
    """Preserve a user-safe LLM error across internal HTTP/A2A boundaries."""
    if response.status_code not in {502, 503}:
        return
    try:
        body = response.json()
    except ValueError:
        return
    code = body.get("code")
    if code not in {"llm_unavailable", "llm_invalid_response"}:
        return
    raise LLMServiceError(
        str(body.get("detail") or "ИИ-модель недоступна"),
        code=str(code),
        status_code=response.status_code,
    )


def _validation_error_summary(exc: Exception) -> str:
    """Describe schema failures without echoing model output or prompt data."""
    if isinstance(exc, ValidationError):
        issues = [
            {
                "location": ".".join(str(part) for part in issue.get("loc", ())),
                "type": issue.get("type"),
                "message": issue.get("msg"),
            }
            for issue in exc.errors(include_input=False, include_url=False)[:5]
        ]
        return json.dumps(issues, ensure_ascii=False)
    return type(exc).__name__
