"""Typed contracts for the revel8 Target API communication module.

Requires Pydantic v2. These schemas carry transport instructions and simulator
responses; they do not generate or modify communication content.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ActionType = Literal[
    "send_message", "start_conversation", "send_turn", "end_conversation"
]

# The API may add channels. GET /channels remains the authoritative source.
LIVE_CHANNELS = frozenset({"phone_call", "video_call", "teams_call", "live_chat"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)


class SenderInfo(StrictModel):
    name: str | None = None
    address: str | None = None

    @model_validator(mode="after")
    def require_visible_identity(self) -> "SenderInfo":
        if not (self.name or self.address):
            raise ValueError("SenderInfo requires a name or address")
        return self


class AttachmentSpec(StrictModel):
    """A declared filename, optionally with an audio transcript.

    An uploaded asset is referenced by its asset ID as a string instead.
    """

    filename: str = Field(min_length=1)
    transcript: str | None = None


Attachment = str | AttachmentSpec


class MessageRequest(StrictModel):
    """POST /sessions/{sid}/messages"""

    to: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    sender: SenderInfo | None = Field(default=None, alias="from")
    subject: str | None = None
    body: str = Field(min_length=1)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_channel(self) -> "MessageRequest":
        if self.channel in LIVE_CHANNELS:
            raise ValueError("Live channels must use ConversationStartRequest")
        return self


class ConversationStartRequest(StrictModel):
    """POST /sessions/{sid}/conversations"""

    to: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    sender: SenderInfo | None = Field(default=None, alias="from")

    @model_validator(mode="after")
    def check_channel(self) -> "ConversationStartRequest":
        if self.channel not in LIVE_CHANNELS:
            raise ValueError(f"Expected a live conversation channel: {sorted(LIVE_CHANNELS)}")
        return self


class ConversationTurnRequest(StrictModel):
    """POST /conversations/{cid}/turns"""

    say: str = Field(min_length=1, max_length=4000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)


class PlannedAction(StrictModel):
    """Input produced by the Planning Agent.

    Only fields needed by the particular action are required. The future
    Communication Agent will translate this into the API request schemas.
    """

    action_type: ActionType
    target_id: str | None = None
    channel: str | None = None
    sender: SenderInfo | None = None
    subject: str | None = None
    body: str | None = None
    say: str | None = None
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_action(self) -> "PlannedAction":
        kind = self.action_type
        if kind == "send_message":
            if not self.target_id or not self.channel or not self.body:
                raise ValueError("send_message requires target_id, channel, and body")
            if self.channel in LIVE_CHANNELS:
                raise ValueError("send_message cannot use a live conversation channel")
        elif kind == "start_conversation":
            if not self.target_id or self.channel not in LIVE_CHANNELS:
                raise ValueError("start_conversation requires target_id and a live channel")
        elif kind == "send_turn":
            if not self.say:
                raise ValueError("send_turn requires say")
            if len(self.say) > 4000:
                raise ValueError("Conversation turns are limited to 4,000 characters")
            if len(self.attachments) > 5:
                raise ValueError("Conversation turns allow at most 5 attachments")
        return self


class ExecutionContext(StrictModel):
    """Runtime information owned by the orchestrator.

    request_id should be stable across retries or process recovery.
    Allowed channels / budgets are advisory snapshots; the API is authoritative.
    """

    session_id: str = Field(min_length=1)
    conversation_id: str | None = None
    request_id: str | None = None
    remaining_touches: int | None = Field(default=None, ge=0)
    allowed_channels: list[str] = Field(default_factory=list)
    remaining_conversation_turns: int | None = Field(default=None, ge=0)


class CommunicationResult(StrictModel):
    """Normalized result for the orchestrator, produced by the future agent.py."""

    success: bool
    action_type: ActionType
    session_id: str
    message_id: str | None = None
    conversation_id: str | None = None
    conversation_status: str | None = None
    persona_says: str | None = None
    ended: bool | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    touches_used: int | None = None
    turns_used: int | None = None
    error: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)
