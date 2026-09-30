from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

try:
    from langfuse import get_client
except ImportError:  # pragma: no cover - lets minimal tooling import the project
    get_client = None


class NullObservation:
    def update(self, **_: Any) -> None:
        return None


@contextmanager
def observation(
    name: str,
    *,
    input: Any | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
):
    """Best-effort Langfuse v4 observation; telemetry never breaks banking flows."""
    if get_client is None or not os.getenv("LANGFUSE_PUBLIC_KEY"):
        yield NullObservation()
        return
    try:
        client = get_client()
        manager = client.start_as_current_observation(
            name=name,
            as_type="span",
            input=input,
            metadata=metadata,
        )
    except Exception:  # pragma: no cover - depends on external collector
        logger.exception("Could not start Langfuse observation")
        yield NullObservation()
        return

    with manager as span:
        trace_fields = {
            key: value
            for key, value in {"user_id": user_id, "session_id": session_id}.items()
            if value
        }
        if trace_fields:
            try:
                span.update_trace(**trace_fields)
            except (AttributeError, TypeError):
                span.update(metadata={**(metadata or {}), **trace_fields})
        yield span
