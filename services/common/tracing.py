from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

try:
    from langfuse import get_client, propagate_attributes
except ImportError:  # pragma: no cover - lets minimal tooling import the project
    get_client = None
    propagate_attributes = None


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
    agent_id: str | None = None,
):
    """Create an observation and propagate trace identity to all nested observations.

    Langfuse v4 no longer treats ``span.update_trace`` as the supported way to
    set trace attributes.  ``propagate_attributes`` writes user/session fields
    to the current trace and propagates agent metadata to child LLM spans.
    """
    if (
        get_client is None
        or propagate_attributes is None
        or not os.getenv("LANGFUSE_PUBLIC_KEY")
    ):
        yield NullObservation()
        return
    source_metadata = metadata or {}
    resolved_agent_id = agent_id or _optional_string(source_metadata.get("agent_id"))
    trace_metadata = dict(source_metadata)
    if resolved_agent_id:
        trace_metadata["agent_id"] = resolved_agent_id
    if user_id:
        # user_id is also a first-class Langfuse trace attribute. Keeping it in
        # metadata makes the identity explicit on exported child observations.
        trace_metadata["user_id"] = user_id
    try:
        client = get_client()
        manager = client.start_as_current_observation(
            name=name,
            as_type="span",
            input=input,
            metadata=trace_metadata or None,
        )
    except Exception:  # pragma: no cover - depends on external collector
        logger.exception("Could not start Langfuse observation")
        yield NullObservation()
        return

    with manager as span:
        tags = [f"agent:{resolved_agent_id}"] if resolved_agent_id else None
        with propagate_attributes(
            user_id=user_id,
            session_id=session_id,
            metadata=trace_metadata or None,
            tags=tags,
            trace_name=name,
        ):
            yield span


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None
