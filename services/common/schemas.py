from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["transfer", "service_payment", "investment_buy"]
    parameters: dict[str, Any]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(min_length=3, max_length=128)
    action: AgentAction | None = None
    confirm_action_id: str | None = None
    travel: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    conversation_id: str
    intent: str
    tool_calls: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class SharedMemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=12000)
    memory_type: Literal["semantic", "procedural"] = "procedural"


class ExternalMemoryIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)


class VulnerableAbsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["get_customer_info", "get_customer_accounts", "get_operations_history"]
    customer_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)
