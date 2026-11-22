"""Persisted state for the offline and practice Target API QA orchestrators."""
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
    """Single-session checkpoint. Store request IDs before transmitting contacts."""

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
    session_status: str = "active"
    allowed_channels: list[str] = Field(default_factory=list)
    research_report_path: str | None = None
    last_planner_decision: dict[str, Any] | None = None
    pending_request_id: str | None = None
    pending_action: dict[str, Any] | None = None
    last_evaluation: dict[str, Any] | None = None
    last_error: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def check_consistency(self) -> "RunState":
        if self.touches_used > self.touch_budget:
            raise ValueError("touches_used must not exceed touch_budget")
        if self.conversation_active and not self.conversation_id:
            raise ValueError("active conversation requires conversation_id")
        if (self.pending_request_id is None) != (self.pending_action is None):
            raise ValueError("pending request ID and pending action must both be set or both absent")
        return self

    @property
    def remaining_touches(self) -> int:
        return max(self.touch_budget - self.touches_used, 0)

    @property
    def remaining_turns(self) -> int:
        return max(10 - self.turns_used, 0) if self.conversation_active else 0

    def mark(self, phase: Phase, *, error: str | None = None) -> None:
        self.phase = phase
        self.last_error = error
        self.updated_at = datetime.now(timezone.utc).isoformat()
