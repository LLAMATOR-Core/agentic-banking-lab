from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

PROFILE_FIELDS = ("full_name", "gender", "client_type", "vip")


def time_context(now: datetime | None = None) -> dict[str, str]:
    timezone = os.getenv("BANK_TIMEZONE", "Europe/Moscow")
    current = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    return {
        "current_date": current.date().isoformat(),
        "current_datetime": current.isoformat(),
        "timezone": timezone,
    }


def llm_context(state: dict[str, Any]) -> dict[str, Any]:
    """Project per-turn context without credentials, contact details or silent truncation."""
    profile = state.get("customer_profile") or {}
    return {
        **(state.get("runtime_context") or time_context()),
        "customer_profile": {key: profile[key] for key in PROFILE_FIELDS if key in profile},
        "conversation_history": [
            {key: item[key] for key in ("role", "content", "created_at") if key in item}
            for item in state.get("conversation_history", [])
            if item.get("role") in {"user", "assistant"}
        ],
        "memory_context": [
            {
                "id": item.get("id"),
                "memory_type": item.get("memory_type"),
                "content": item.get("content", ""),
                "source": (item.get("metadata") or {}).get("source"),
                "trust": (item.get("metadata") or {}).get("trust", "unspecified"),
            }
            for item in state.get("memories", [])
        ],
    }
