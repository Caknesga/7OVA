"""Pre-flight validation for actions sent to the revel8 AI-twin simulator.

This module does NOT write, modify, or optimize messages. It only checks that
an orchestrator-supplied action is structurally valid and can be routed to the
simulator under the currently reported session/channel constraints.

Use::

    async with TargetAPIClient() as client:
        validator = CommunicationValidator(client)
        validated = await validator.validate(planned_action, execution_context)

Validation is not a reservation: the simulator remains authoritative when an
HTTP request is actually submitted. Do not run concurrent writes to a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from .client import TargetAPIClient
from .schemas import (
    ConversationStartRequest,
    ConversationTurnRequest,
    ExecutionContext,
    MessageRequest,
    PlannedAction,
)


class ActionValidationError(ValueError):
    """A planned action cannot safely be routed to the simulator."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


RequestModel = MessageRequest | ConversationStartRequest | ConversationTurnRequest | None


@dataclass(frozen=True)
class ValidatedAction:
    """Validated transport parameters, without making a contact."""

    action: PlannedAction
    context: ExecutionContext
    payload: RequestModel
    session: dict[str, Any]


class CommunicationValidator:
    """Validate planner actions against session state and API channel metadata.

    The channel catalogue is cached for the lifetime of the validator because
    it seldom changes. Call ``refresh_channels`` if organizers add a channel.
    Scenario/target visibility and session state are fetched on each validation
    so newly opened evaluation targets are not confused with practice targets.
    """

    def __init__(self, client: TargetAPIClient) -> None:
        self.client = client
        self._channels: dict[str, dict[str, Any]] | None = None

    async def refresh_channels(self) -> None:
        data = await self.client.get_channels()
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict) and isinstance(data.get("channels"), list):
            entries = data["channels"]
        else:
            raise ActionValidationError(
                "INVALID_CHANNELS_RESPONSE", "Unexpected response from GET /channels"
            )

        channels: dict[str, dict[str, Any]] = {}
        for item in entries:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                channels[item["id"]] = item
        if not channels:
            raise ActionValidationError(
                "EMPTY_CHANNELS", "The simulator returned no usable channels"
            )
        self._channels = channels

    async def validate(
        self,
        planned_action: PlannedAction | dict[str, Any],
        execution_context: ExecutionContext | dict[str, Any],
    ) -> ValidatedAction:
        """Validate an action, without sending any message or using a touch.

        Raises ActionValidationError for input, session, budget, target or
        channel problems. Network/API errors from TargetAPIClient propagate.
        """
        try:
            action = PlannedAction.model_validate(planned_action)
            context = ExecutionContext.model_validate(execution_context)
        except PydanticValidationError as exc:
            raise ActionValidationError("INVALID_INPUT", str(exc)) from exc

        # The same ID must be retained after timeouts, restarts, or retries.
        if action.action_type in {"send_message", "start_conversation", "send_turn"}:
            if not context.request_id or not context.request_id.strip():
                raise ActionValidationError(
                    "MISSING_REQUEST_ID",
                    "The orchestrator must assign a stable unique request_id per action",
                )

        if action.action_type in {"send_turn", "end_conversation"}:
            if not context.conversation_id:
                raise ActionValidationError(
                    "MISSING_CONVERSATION_ID",
                    f"{action.action_type} requires execution_context.conversation_id",
                )
        if action.action_type == "send_turn" and context.remaining_conversation_turns == 0:
            raise ActionValidationError(
                "NO_TURNS_LEFT", "The conversation has no remaining turns"
            )

        session = await self.client.get_session(context.session_id)
        if not isinstance(session, dict):
            raise ActionValidationError("INVALID_SESSION", "Unexpected session response")
        if session.get("session_id") not in (None, context.session_id):
            raise ActionValidationError(
                "SESSION_MISMATCH", "The API returned a different session ID"
            )
        if session.get("status") != "active":
            raise ActionValidationError(
                "SESSION_NOT_ACTIVE", "The simulator session is not active"
            )

        if action.action_type in {"send_message", "start_conversation"}:
            remaining = _remaining_touches(session)
            if remaining is not None and remaining <= 0:
                raise ActionValidationError(
                    "TOUCH_BUDGET_EXHAUSTED", "The API session has no touches left"
                )
            if context.remaining_touches == 0:
                raise ActionValidationError(
                    "TOUCH_BUDGET_EXHAUSTED", "Orchestrator reports no touches left"
                )

        payload: RequestModel = None
        if action.action_type in {"send_message", "start_conversation"}:
            if self._channels is None:
                await self.refresh_channels()
            assert self._channels is not None
            channel = self._channels.get(action.channel or "")
            if channel is None:
                raise ActionValidationError(
                    "UNKNOWN_CHANNEL", f"Channel {action.channel!r} was not found in GET /channels"
                )
            if context.allowed_channels and action.channel not in context.allowed_channels:
                raise ActionValidationError(
                    "CHANNEL_NOT_ALLOWED", "Channel excluded by the orchestrator"
                )
            expected_kind = "message" if action.action_type == "send_message" else "live"
            if channel.get("kind") != expected_kind:
                raise ActionValidationError(
                    "WRONG_CHANNEL_KIND",
                    f"{action.channel!r} is not a {expected_kind} channel",
                )
            await self._check_target(session, action.target_id)

            if action.action_type == "send_message":
                payload = MessageRequest(
                    to=action.target_id,
                    channel=action.channel,
                    sender=action.sender,
                    subject=action.subject,
                    body=action.body,
                    attachments=action.attachments,
                )
            else:
                if action.attachments:
                    raise ActionValidationError(
                        "UNSUPPORTED_ATTACHMENTS",
                        "Conversation start does not accept attachments; send them in turns",
                    )
                payload = ConversationStartRequest(
                    to=action.target_id,
                    channel=action.channel,
                    sender=action.sender,
                )
        elif action.action_type == "send_turn":
            payload = ConversationTurnRequest(
                say=action.say, attachments=action.attachments
            )
        # end_conversation has no request payload.

        return ValidatedAction(action, context, payload, session)

    async def _check_target(self, session: dict[str, Any], target_id: str | None) -> None:
        scenario_id = session.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ActionValidationError(
                "MISSING_SCENARIO", "Session did not provide a scenario_id"
            )
        scenarios = await self.client.get_scenarios()
        if not isinstance(scenarios, list):
            raise ActionValidationError(
                "INVALID_SCENARIOS_RESPONSE", "Unexpected GET /scenarios response"
            )
        scenario = next(
            (s for s in scenarios if isinstance(s, dict) and s.get("id") == scenario_id),
            None,
        )
        if scenario is None:
            raise ActionValidationError(
                "UNKNOWN_SCENARIO", f"Session scenario {scenario_id!r} is not visible"
            )
        if scenario.get("open") is False:
            raise ActionValidationError(
                "SCENARIO_CLOSED", f"Scenario {scenario_id!r} is closed"
            )
        targets = scenario.get("targets")
        if not isinstance(targets, list):
            raise ActionValidationError(
                "TARGETS_NOT_AVAILABLE", "Scenario has not revealed its target list"
            )
        valid_ids = {t.get("id") for t in targets if isinstance(t, dict)}
        if target_id not in valid_ids:
            raise ActionValidationError(
                "UNKNOWN_TARGET", f"Target {target_id!r} is not in scenario {scenario_id!r}"
            )


def _remaining_touches(session: dict[str, Any]) -> int | None:
    """Return API budget remainder, if both count fields are present."""
    budget = session.get("touch_budget")
    used = session.get("touches_used")
    if budget is None or used is None:
        return None
    if isinstance(budget, bool) or isinstance(used, bool):
        raise ActionValidationError("INVALID_BUDGET", "Boolean touch count returned")
    try:
        total, consumed = int(budget), int(used)
    except (TypeError, ValueError) as exc:
        raise ActionValidationError("INVALID_BUDGET", "Invalid touch count returned") from exc
    if total < 0 or consumed < 0:
        raise ActionValidationError("INVALID_BUDGET", "Negative touch count returned")
    return total - consumed
