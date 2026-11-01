from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class MockFlightSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    currency: str = Field(default="rub", pattern=r"^[a-zA-Z]{3}$")
    max_results: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_trip(self) -> MockFlightSearch:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.departure_date < date.today():
            raise ValueError("departure_date cannot be in the past")
        if self.return_date is not None and self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        return self


class VulnerableAbsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["get_customer_info", "get_customer_accounts", "get_operations_history"]
    customer_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)
