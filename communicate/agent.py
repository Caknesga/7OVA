"""Communication execution layer for the revel8 *simulated* Target API.

This module does not research targets, create messages, choose channels, or
contact real-world services. It executes an already prepared PlannedAction,
validates against the simulator, and normalizes the simulator response.

Usage::

    async with TargetAPIClient() as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(planned_action, execution_context)

For each *logical* contact, keep execution_context.request_id unchanged when
retrying after timeouts. Do not concurrently submit actions in one session.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from .client import TargetAPIClient, TargetAPIError
from .schemas import (
    CommunicationResult,
    ConversationStartRequest,
    ConversationTurnRequest,
    ExecutionContext,
    MessageRequest,
    PlannedAction,
)
from .validator import ActionValidationError, CommunicationValidator


class SimulatorResponseError(ValueError):
    """The Target API returned a successful but unusable response."""


class CommunicationAgent:
    """Validate, execute, and report one planner action at a time.

    The caller owns the TargetAPIClient and its lifecycle. The per-session
    asyncio locks prevent this *agent instance* from concurrently executing
    requests in the same simulation session; they do not coordinate processes.
    """

    def __init__(
        self,
        client: TargetAPIClient,
        *,
        validator: CommunicationValidator | None = None,
    ) -> None:
        self.client = client
        self.validator = validator or CommunicationValidator(client)
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute(
        self,
        planned_action: PlannedAction | dict[str, Any],
        execution_context: ExecutionContext | dict[str, Any],
    ) -> CommunicationResult:
        """Execute an action and return a normalized result.

        Invalid *input shapes* raise ``pydantic.ValidationError``. For a
        well-formed action, validation failures, API errors, and malformed
        simulator responses become ``CommunicationResult(success=False, ...)``.

        The agent never modifies a message's wording or produces a follow-up.
        """
        action = PlannedAction.model_validate(planned_action)
        context = ExecutionContext.model_validate(execution_context)

        # The same lock covers state validation and sending a contact.
        async with self._locks.setdefault(context.session_id, asyncio.Lock()):
            try:
                checked = await self.validator.validate(action, context)
                response = await self._dispatch(
                    action.action_type,
                    checked.payload,
                    context,
                )
                return self._normalize(action.action_type, context, response)
            except (ActionValidationError, TargetAPIError, SimulatorResponseError) as exc:
                return CommunicationResult(
                    success=False,
                    action_type=action.action_type,
                    session_id=context.session_id,
                    conversation_id=context.conversation_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def _dispatch(
        self,
        action_type: str,
        payload: MessageRequest | ConversationStartRequest | ConversationTurnRequest | None,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        if action_type == "send_message":
            assert isinstance(payload, MessageRequest)
            return await self.client.send_message(
                context.session_id, payload, request_id=context.request_id
            )
        if action_type == "start_conversation":
            assert isinstance(payload, ConversationStartRequest)
            return await self.client.start_conversation(
                context.session_id, payload, request_id=context.request_id
            )
        if action_type == "send_turn":
            assert isinstance(payload, ConversationTurnRequest)
            assert context.conversation_id is not None
            return await self.client.send_turn(
                context.conversation_id, payload, request_id=context.request_id
            )
        if action_type == "end_conversation":
            assert context.conversation_id is not None
            return await self.client.end_conversation(context.conversation_id)
        raise ValueError(f"Unhandled action_type: {action_type}")

    @staticmethod
    def _normalize(
        action_type: str,
        context: ExecutionContext,
        response: Any,
    ) -> CommunicationResult:
        if not isinstance(response, dict):
            raise SimulatorResponseError("Expected a JSON object from Target API")

        expected_key = {
            "send_message": "message_id",
            "start_conversation": "conversation_id",
        }.get(action_type)
        if expected_key and not response.get(expected_key):
            raise SimulatorResponseError(
                f"Response to {action_type} omitted required field {expected_key!r}"
            )

        events = response.get("events", [])
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            raise SimulatorResponseError("Expected events to be a list of objects")

        # End-conversation may return an empty object on HTTP success.
        ended = response.get("ended")
        if action_type == "end_conversation" and ended is None:
            ended = True

        try:
            return CommunicationResult(
                success=True,
                action_type=action_type,
                session_id=context.session_id,
                message_id=response.get("message_id"),
                conversation_id=response.get("conversation_id") or context.conversation_id,
                conversation_status=response.get("status"),
                persona_says=response.get("persona_says"),
                ended=ended,
                events=events,
                # API touches_used is cumulative within the session, NOT
                # necessarily the cost of this single call.
                touches_used=response.get("touches_used"),
                turns_used=response.get("turns_used"),
                raw_response=response,
            )
        except ValidationError as exc:
            raise SimulatorResponseError(f"Invalid simulator response: {exc}") from exc
