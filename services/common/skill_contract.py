from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.common.banking_policy import AGENT_SCOPES

SKILLS = {
    "operations": ("operations-skill", " ".join(sorted(AGENT_SCOPES["operations-skill"]))),
    "investments": ("investment-skill", "portfolio.read"),
    "travel": ("travel-skill", "travel.plan"),
}
SKILL_CLIENTS = {item[0] for item in SKILLS.values()}


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str = Field(
        default="interactive", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    run_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")


class SkillTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(min_length=3, max_length=128)
    experiment: Experiment
    travel: dict[str, Any] | None = None
    abs_customer_id: str | None = Field(default=None, min_length=1, max_length=64)


class SkillMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=3, max_length=128)
    experiment: Experiment
    query: str = Field(min_length=1, max_length=4000)


class SkillMemoryCandidate(SkillMemoryRequest):
    content: str = Field(min_length=1, max_length=12000)
    source: Literal[
        "bank-operations",
        "bank-portfolio",
        "bank-profile",
        "bank-accounts",
        "bank-products",
        "bank-statement",
        "internet",
        "travel-partner",
        "skill-answer",
    ]


class SkillMemoryTurn(SkillMemoryRequest):
    assistant_message: str = Field(min_length=1, max_length=4000)
    travel_state: dict[str, Any] | None = Field(default=None, max_length=20)
