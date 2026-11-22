"""Deterministic state for an offline QA orchestrator.

No network operations, credentials, or message-generation logic belong here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Phase(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    STOPPED = "stopped"
    FAILED = "failed"


class RunState(BaseModel):
    """One run, including enough state to resume or audit its dry-run steps."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    scenario_id: str = Field(default="offline_qa", min_length=1)
    target_id: str = Field(default="test_persona", min_length=1)
    phase: Phase = Phase.CREATED
    touch_budget: int = Field(default=10, ge=0)
    touches_used: int = Field(default=0, ge=0)
    steps_completed: int = Field(default=0, ge=0)
    conversation_id: str | None = None
    conversation_active: bool = False
    turns_used: int = Field(default=0, ge=0, le=10)
    last_evaluation: dict[str, Any] | None = None
    last_error: str | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def check_budget(self) -> "RunState":
        if self.touches_used > self.touch_budget:
            raise ValueError("touches_used must not exceed touch_budget")
        if self.conversation_active and not self.conversation_id:
            raise ValueError("active conversation needs conversation_id")
        return self

    @property
    def remaining_touches(self) -> int:
        return self.touch_budget - self.touches_used

    @property
    def remaining_turns(self) -> int:
        return 10 - self.turns_used if self.conversation_active else 0

    def mark(self, phase: Phase, *, error: str | None = None) -> None:
        self.phase = phase
        self.last_error = error
        self.updated_at = datetime.now(timezone.utc).isoformat()
