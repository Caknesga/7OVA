"""Typed, evidence-based evaluation contract for simulated communications.

This module contains no message generation, persuasion scoring, or contact decisions.
It evaluates the correctness and observability of a recorded simulator workflow.
Requires Pydantic >= 2.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventCounts(StrictModel):
    reply: int = Field(default=0, ge=0)
    link_opened: int = Field(default=0, ge=0)
    page_submitted: int = Field(default=0, ge=0)
    attachment_opened: int = Field(default=0, ge=0)
    other: int = Field(default=0, ge=0)


class InteractionSummary(StrictModel):
    step: int = Field(ge=1)
    action_type: str
    channel: str | None = None
    success: bool
    outgoing_logged: bool
    message_id: str | None = None
    conversation_id: str | None = None
    event_types: list[str] = Field(default_factory=list)
    persona_response_observed: bool = False
    touches_used: int | None = Field(default=None, ge=0)
    error: str | None = None


class EvaluationMetrics(StrictModel):
    actions_logged: int = Field(ge=0)
    successful_actions: int = Field(ge=0)
    failed_actions: int = Field(ge=0)
    messages_sent: int = Field(ge=0)
    conversations_started: int = Field(ge=0)
    conversation_turns_sent: int = Field(ge=0)
    conversations_ended: int = Field(ge=0)
    actions_with_observable_response: int = Field(ge=0)
    persona_utterances: int = Field(ge=0)
    events: EventCounts
    touch_budget: int | None = Field(default=None, ge=0)
    touches_used: int | None = Field(default=None, ge=0)
    remaining_touches: int | None = Field(default=None, ge=0)
    transport_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class OrchestratorFeedback(StrictModel):
    control: Literal["decision_required", "review", "stop"]
    reason: str
    session_status: str | None = None
    conversation_active: bool | None = None
    # This is a technical gate, not a recommendation for what to say or whom to contact.


class EvaluationReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str = Field(min_length=1)
    evaluated_at: str
    evaluation_status: Literal["completed", "partial", "no_data"]
    metrics: EvaluationMetrics
    timeline: list[InteractionSummary] = Field(default_factory=list)
    orchestrator_feedback: OrchestratorFeedback
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
