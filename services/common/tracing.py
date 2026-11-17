from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import date, datetime
from functools import wraps
from typing import Any

from opentelemetry.baggage import set_baggage
from opentelemetry.trace import format_span_id, format_trace_id, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel
from starlette.responses import Response

logger = logging.getLogger(__name__)

try:
    from langfuse import get_client, propagate_attributes
    from opentelemetry.context import Context, attach, detach
except ImportError:  # pragma: no cover - lets minimal tooling import the project
    get_client = None
    propagate_attributes = None


_experiment = ContextVar("experiment", default=None)
_upstream_trace = ContextVar("upstream_trace", default=None)
_request_trace_enabled = ContextVar("request_trace_enabled", default=True)
MEMORY_LAYERS = frozenset({"working", "episodic", "semantic", "procedural"})


@contextmanager
def experiment_context(metadata):
    marker = _experiment.set(dict(metadata))
    try:
        yield
    finally:
        _experiment.reset(marker)


class NullObservation:
    def update(self, **_: Any) -> None:
        return None


def trace_value(value: Any) -> Any:
    """Snapshot payloads without credentials or live mutable application objects.

    Do not shorten content: the lab needs complete model/tool/memory evidence.
    JSON encoded in message content is inspected too, so serializing a payload
    before passing it to a model does not bypass credential redaction.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Response):
        try:
            return trace_value(json.loads(value.body))
        except (ValueError, AttributeError):
            return {"status_code": value.status_code}
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _secret_key(str(key)) else trace_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [trace_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(trace_value(json.loads(value)), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in {"authorization", "password", "secret", "cvv", "card_number"} or (
        normalized == "token" or normalized.endswith(("_token", "_password", "_secret", "api_key"))
    )


class RecordedObservation:
    """Keep telemetry failures from breaking business calls; redact every update."""

    def __init__(self, span: Any) -> None:
        self.span = span
        self.has_output = False

    def update(self, **kwargs: Any) -> None:
        self.has_output = self.has_output or "output" in kwargs
        try:
            self.span.update(**trace_value(kwargs))
        except Exception:
            logger.exception("Could not update Langfuse observation")


def public_trace_config() -> dict[str, str | None]:
    return {
        "system_id": os.getenv("AGENT_SYSTEM_ID"),
        "project_name": os.getenv("LANGFUSE_PROJECT_NAME"),
        "project_id": os.getenv("LANGFUSE_PROJECT_ID"),
    }


@contextmanager
def observation(
    name: str,
    *,
    input: Any | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    system_boundary: bool = False,
    as_type: str = "span",
    model: str | None = None,
    model_parameters: dict[str, Any] | None = None,
    memory_layer: str | None = None,
    memory_layers: list[str] | None = None,
):
    """Create an observation and propagate trace identity to all nested observations.

    Langfuse v4 no longer treats ``span.update_trace`` as the supported way to
    set trace attributes.  ``propagate_attributes`` writes user/session fields
    to the current trace and propagates agent metadata to child LLM spans.
    """
    if (
        not _request_trace_enabled.get()
        or get_client is None
        or propagate_attributes is None
        or not os.getenv("LANGFUSE_PUBLIC_KEY")
    ):
        yield NullObservation()
        return
    source_metadata = {
        **(_experiment.get() or {}),
        **(_upstream_trace.get() or {}),
        **(metadata or {}),
    }
    resolved_agent_id = agent_id or _optional_string(source_metadata.get("agent_id"))
    trace_metadata = dict(source_metadata)
    if memory_layer in MEMORY_LAYERS:
        trace_metadata["memory_layer"] = memory_layer
    if memory_layers is not None:
        trace_metadata["memory_layers"] = [
            layer for layer in memory_layers if layer in MEMORY_LAYERS
        ]
    system_id = os.getenv("AGENT_SYSTEM_ID")
    if system_id:
        trace_metadata["system_id"] = system_id
    if session_id:
        trace_metadata["correlation_id"] = session_id
    if resolved_agent_id:
        trace_metadata["agent_id"] = resolved_agent_id
    if user_id:
        # user_id is also a first-class Langfuse trace attribute. Keeping it in
        # metadata makes the identity explicit on exported child observations.
        trace_metadata["user_id"] = user_id
    # A service boundary is a child of the incoming request, not a new trace.
    is_root = not get_current_span().get_span_context().is_valid
    if system_boundary:
        trace_metadata["system_boundary"] = True
    try:
        client = get_client()
        attributes = {}
        if model is not None:
            attributes["model"] = model
        if model_parameters is not None:
            attributes["model_parameters"] = trace_value(model_parameters)
        manager = client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=trace_value(input),
            metadata=trace_value(trace_metadata) or None,
            **attributes,
        )
    except Exception:  # pragma: no cover - depends on external collector
        logger.exception("Could not start Langfuse observation")
        yield NullObservation()
        return

    with ExitStack() as stack:
        tags = [f"system:{system_id}"] if system_id else []
        if resolved_agent_id:
            tags.append(f"agent:{resolved_agent_id}")
        if memory_layer in MEMORY_LAYERS:
            tags.append(f"memory:{memory_layer}")
        try:
            span = stack.enter_context(manager)
            stack.enter_context(
                propagate_attributes(
                    user_id=user_id,
                    session_id=session_id,
                    metadata=trace_value(trace_metadata) or None,
                    tags=tags,
                    trace_name=name if is_root else None,
                )
            )
        except Exception:
            logger.exception("Could not enter Langfuse observation")
            yield NullObservation()
            return
        recorded = RecordedObservation(span)
        try:
            yield recorded
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            if hasattr(exc, "detail"):
                error["detail"] = trace_value(exc.detail)
            recorded.update(level="ERROR", status_message=json.dumps(error, ensure_ascii=False))
            if not recorded.has_output:
                recorded.update(output={"error": error})
            raise


def traced(
    name: str,
    *,
    as_type: str = "span",
    system_boundary: bool = False,
    agent_id: str | None = None,
    memory_layer: str | None = None,
):
    """Trace async graph steps and validated API handlers, preserving their signatures."""

    def decorate(function):
        signature = inspect.signature(function, eval_str=True)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            inputs = {
                key: value
                for key, value in bound.arguments.items()
                if key not in {"self", "cls", "caller"} and not hasattr(value, "authorized_party")
            }
            context = dict(inputs)
            for key in ("state", "payload", "request", "source", "job"):
                value = inputs.get(key)
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode="json")
                if isinstance(value, dict):
                    context.update(value)
            caller = bound.arguments.get("caller")
            message = context.get("message")
            session_id = context.get("conversation_id")
            if isinstance(message, dict):
                session_id = session_id or message.get("contextId")
            instance = bound.arguments.get("self")
            endpoint = getattr(instance, "endpoint", None)
            with observation(
                name,
                input=inputs,
                as_type=as_type,
                system_boundary=system_boundary,
                agent_id=agent_id,
                user_id=context.get("user_id") or getattr(caller, "customer_id", None),
                session_id=session_id,
                metadata={"endpoint": endpoint} if endpoint else None,
                memory_layer=memory_layer or context.get("memory_type"),
                memory_layers=context.get("memory_types"),
            ) as span:
                result = await function(*args, **kwargs)
                span.update(output=result)
                return result

        # FastAPI and LangGraph inspect annotations outside the original module.
        wrapped.__signature__ = signature
        wrapped.__annotations__ = inspect.get_annotations(function, eval_str=True)
        return wrapped

    return decorate


def trace_headers() -> dict[str, str]:
    """Propagate only the W3C parent to downstreams, never auth or user baggage."""
    headers: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(headers)
    if headers and os.getenv("AGENT_SYSTEM_ID"):
        headers["x-agent-system-id"] = os.environ["AGENT_SYSTEM_ID"]
    for key, value in (_experiment.get() or {}).items():
        if key in {"experiment_id", "run_id"}:
            headers["x-" + key.replace("_", "-")] = str(value)
    return headers


def install_experiment_middleware(
    app,
    *,
    accept_remote_parent: bool = True,
    agent_routes: dict[tuple[str, str], str] | None = None,
    trace_mcp_calls: bool = False,
):
    """Trace agent entrypoints and joined MCP tool calls, not routine HTTP traffic."""

    @app.middleware("http")
    async def labels(request, call_next):
        metadata = {}
        for key in ("experiment_id", "run_id"):
            value = request.headers.get("x-" + key.replace("_", "-"))
            if value and len(value) <= 100 and value.isascii() and value.isprintable():
                metadata[key] = value
        # Labels are correlation data only; they never participate in authorization.
        with (
            independent_trace(),
            mcp_parent(request.headers) if accept_remote_parent else nullcontext(),
            experiment_context(metadata),
        ):
            route_name = (agent_routes or {}).get((request.method, request.url.path))
            mcp_call = trace_mcp_calls and request.method == "POST" and request.url.path == "/mcp"
            payload = None
            if route_name or mcp_call:
                if "application/json" in request.headers.get("content-type", "") or (
                    "application/a2a+json" in request.headers.get("content-type", "")
                ):
                    try:
                        payload = await request.json()
                    except ValueError:
                        pass
            joined_mcp_call = (
                mcp_call
                and isinstance(payload, dict)
                and payload.get("method") == "tools/call"
                and get_current_span().get_span_context().is_valid
                and request.headers.get("x-agent-system-id") == os.getenv("AGENT_SYSTEM_ID")
            )
            enabled = bool(route_name or joined_mcp_call)
            marker = _request_trace_enabled.set(enabled)
            try:
                if not route_name:
                    return await call_next(request)
                correlation = payload.get("conversation_id") if isinstance(payload, dict) else None
                if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
                    correlation = correlation or payload["message"].get("contextId")
                with observation(
                    route_name,
                    input={"method": request.method, "path": request.url.path, "body": payload},
                    session_id=correlation,
                    system_boundary=True,
                ) as span:
                    response = await call_next(request)
                    span.update(
                        output={"status_code": response.status_code},
                        **({"level": "ERROR"} if response.status_code >= 400 else {}),
                    )
                    return response
            finally:
                _request_trace_enabled.reset(marker)


@contextmanager
def mcp_parent(headers: Mapping[str, str]):
    """Join local-system calls; link foreign traces without exporting their observations."""
    parent = TraceContextTextMapPropagator().extract(dict(headers))
    span_context = get_current_span(parent).get_span_context()
    system_id = os.getenv("AGENT_SYSTEM_ID")
    if span_context.is_valid and headers.get("x-agent-system-id") == system_id:
        # Langfuse v4 otherwise marks the first span in each process as an app root.
        # Derive this marker locally; never import arbitrary incoming baggage.
        parent = set_baggage("langfuse_trace_id", format_trace_id(span_context.trace_id), parent)
        token = attach(parent)
        try:
            yield
        finally:
            detach(token)
    elif span_context.is_valid:
        with independent_trace():
            upstream = _upstream_trace.set(
                {
                    "upstream_trace_id": format_trace_id(span_context.trace_id),
                    "upstream_span_id": format_span_id(span_context.span_id),
                    "upstream_system_id": (headers.get("x-agent-system-id") or "unknown")[:100],
                }
            )
            try:
                yield
            finally:
                _upstream_trace.reset(upstream)
    else:
        yield


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


@contextmanager
def independent_trace():
    """Start a receiving system's trace without inheriting the caller's OTel parent/baggage."""
    token = attach(Context())
    upstream = _upstream_trace.set(None)
    try:
        yield
    finally:
        _upstream_trace.reset(upstream)
        detach(token)
