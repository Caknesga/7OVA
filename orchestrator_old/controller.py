"""Offline orchestration, logging and evaluation for a simulator QA workflow.

This controller deliberately uses a local fake communication port. It does not
contact targets or execute an autonomous deceptive outreach loop. The planner
here provides predefined benign actions for testing the integration contracts.

Run from repository root:
    python -m orchestrator.controller
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

try:
    from communicate.schemas import CommunicationResult, ExecutionContext, PlannedAction
except ModuleNotFoundError as exc:
    if exc.name != "communicate":
        raise
    # Local standalone bundle uses communication/; the user repository uses communicate/.
    from communicate.schemas import CommunicationResult, ExecutionContext, PlannedAction
from evaluation.agent import EvaluationAgent
from .state import Phase, RunState


from planning.agent import PlannerInput, PlannerResult

from copy import deepcopy

class PlannerPort(Protocol):
    async def plan(
        self, inputs: PlannerInput
    ) -> PlannerResult:
        """Return a proposed action and its rationale, or None if no action is available."""
        ...


class ScriptedTestPlanner:
    """Finite, prerecorded QA actions; no LLM and no targeting decisions."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = [PlannedAction.model_validate(a) for a in actions]
        self._position = 0

    async def propose(self, state: RunState) -> PlannedAction | None:
        if self._position >= len(self._actions):
            return None
        action = self._actions[self._position]
        self._position += 1
        return action


class OfflineCommunicationPort:
    """Local mock. Never makes an HTTP request; useful for integration tests."""

    def __init__(self) -> None:
        self._touches = 0
        self._turns = 0
        self._conversation_id: str | None = None

    async def execute(
        self, planned_action: PlannedAction | dict[str, Any],
        execution_context: ExecutionContext | dict[str, Any],
    ) -> CommunicationResult:
        action = PlannedAction.model_validate(planned_action)
        context = ExecutionContext.model_validate(execution_context)
        base: dict[str, Any] = {
            "success": True,
            "action_type": action.action_type,
            "session_id": context.session_id,
            "events": [],
            "touches_used": self._touches,
        }
        kind = action.action_type
        if kind in {"send_message", "start_conversation"}:
            if context.remaining_touches is not None and context.remaining_touches < 1:
                return CommunicationResult(**{**base, "success": False, "error": "Touch budget exhausted"})
            self._touches += 1
            base["touches_used"] = self._touches
        if kind == "send_message":
            base["message_id"] = f"mock_message_{self._touches}"
            base["events"] = [{
                "id": f"mock_event_{self._touches}",
                "type": "reply", "body": "Acknowledged: test message received."
            }]
        elif kind == "start_conversation":
            self._conversation_id = f"mock_conversation_{self._touches}"
            self._turns = 0
            base.update(conversation_id=self._conversation_id,
                        conversation_status="answered", persona_says="Hello, QA test ready.")
        elif kind == "send_turn":
            if not self._conversation_id or context.conversation_id != self._conversation_id:
                base.update(success=False, error="No active test conversation")
            elif self._turns >= 10:
                base.update(success=False, error="Conversation turn limit reached")
            else:
                self._turns += 1
                base.update(conversation_id=self._conversation_id, turns_used=self._turns,
                            persona_says="I received the test message.", ended=False)
        elif kind == "end_conversation":
            if not self._conversation_id or context.conversation_id != self._conversation_id:
                base.update(success=False, error="No active test conversation")
            else:
                base.update(conversation_id=self._conversation_id, ended=True)
                self._conversation_id = None
        return CommunicationResult(**base)


class Orchestrator:
    """Controls a bounded OFFLINE test and writes evaluator-compatible logs.

    Uses OfflineCommunicationPort only: no live dispatch is possible through this
    controller. State checkpoints and JSONL events are written to local disk.
    """

    def __init__(
        self,
        planner: PlannerPort,
        *,
        communication: OfflineCommunicationPort | None = None,
        log_dir: str | Path = "logs",
        state_dir: str | Path = "orchestrator/state",
        evaluation_dir: str | Path = "evaluation/results",
        max_steps: int = 10,
    ) -> None:
        if not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be between 1 and 50")
        if communication is not None and type(communication) is not OfflineCommunicationPort:
            raise TypeError("Only the offline QA communication port is supported")
        self.planner = planner
        self.communication = communication or OfflineCommunicationPort()
        self.log_dir = Path(log_dir)
        self.state_dir = Path(state_dir)
        self.evaluator = EvaluationAgent(log_dir=log_dir, output_dir=evaluation_dir)
        self.max_steps = max_steps

    def _checkpoint(self, state: RunState) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        destination = self.state_dir / f"{state.session_id}.json"
        temp = destination.with_suffix(".json.tmp")
        temp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temp, destination)
        return destination

    def _append_log(
        self, state: RunState, action: PlannedAction,
        context: ExecutionContext, result: CommunicationResult,
    ) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{state.session_id}.jsonl"
        entry = {
            "session_id": state.session_id,
            "planned_action": action.model_dump(mode="json", exclude_none=True),
            "execution_context": context.model_dump(mode="json", exclude_none=True),
            "communication_result": result.model_dump(mode="json"),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()

    def _build_planner_input(self, state: RunState) -> PlannerInput:
        if self.planner_inputs is None:
            raise ValueError("Planner input configuration missing")

        data = deepcopy(self.planner_inputs)

        environment = data["simulator_environment"]
        environment.update({
            "session_id": state.session_id,
            "target_id": state.target_id,
            "remaining_touch_budget": state.remaining_touches,
            "conversation_active": state.conversation_active,
            "conversation_id": state.conversation_id,
            "remaining_conversation_turns": state.remaining_turns,
        })

        data["operational_constraints"]["actions_used"] = (
            state.steps_completed
        )

        log_path = self.log_dir / f"{state.session_id}.jsonl"
        entries = []

        if log_path.exists():
            entries = [
                json.loads(line)
                for line in log_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]

        data["interaction_history"] = {
            "previous_actions": [
                e["planned_action"] for e in entries
            ],
            "simulator_replies": [
                {
                    "persona_says": e["communication_result"].get(
                        "persona_says"
                    ),
                    "message_id": e["communication_result"].get(
                        "message_id"
                    ),
                }
                for e in entries
                if e["communication_result"].get("persona_says")
            ] + [
                event
                for e in entries
                for event in e["communication_result"].get("events", [])
                if event.get("type") == "reply"
            ],
            "observable_events": [
                event
                for e in entries
                for event in e["communication_result"].get("events", [])
            ],
            "last_evaluation": state.last_evaluation,
        }

        return PlannerInput.model_validate(data)

    async def run(self, state: RunState) -> RunState:
        """Execute finite canned QA actions and evaluate each step."""
        if state.phase is not Phase.CREATED or state.steps_completed:
            raise ValueError("This offline demo expects a new RunState")
        self._checkpoint(state)
        for _ in range(self.max_steps):
            state.mark(Phase.PLANNING)
            self._checkpoint(state)
            try:
                if hasattr(self.planner, "plan"):
                    # New OpenAI Planning Agent
                    planner_input = self._build_planner_input(state)
                    decision = await self.planner.plan(planner_input)

                    if decision.decision == "stop":
                        state.mark(Phase.COMPLETED)
                        break

                    if decision.decision == "insufficient_information":
                        state.mark(
                            Phase.NEEDS_REVIEW,
                            error=decision.reasoning_summary
                        )
                        break

                    action = decision.planned_action

                else:
                    # Existing ScriptedTestPlanner (offline tests)
                    action = await self.planner.propose(state)

                    if action is None:
                        state.mark(Phase.COMPLETED)
                        break
                # Reject sending when there are no contacts left, but allow an
                # active conversation to be ended after the touch budget expires.
                if action.action_type in {"send_message", "start_conversation"} and state.remaining_touches == 0:
                    state.mark(Phase.STOPPED, error="Touch budget exhausted")
                    break
                if action.action_type == "send_turn" and state.remaining_turns == 0:
                    state.mark(Phase.STOPPED, error="Conversation turn budget exhausted")
                    break
                state.mark(Phase.EXECUTING)
                context = ExecutionContext(
                    session_id=state.session_id,
                    conversation_id=state.conversation_id,
                    request_id=str(uuid4()),
                    remaining_touches=state.remaining_touches,
                    remaining_conversation_turns=state.remaining_turns if state.conversation_active else None,
                )
                result = await self.communication.execute(action, context)
                self._append_log(state, action, context, result)
                state.steps_completed += 1
                if result.touches_used is not None:
                    state.touches_used = result.touches_used
                if result.success:
                    if action.action_type == "start_conversation" and result.conversation_id:
                        state.conversation_id = result.conversation_id
                        state.conversation_active = True
                        state.turns_used = 0
                    elif action.action_type == "send_turn":
                        state.turns_used = result.turns_used if result.turns_used is not None else state.turns_used + 1
                        if result.ended:
                            state.conversation_active = False
                    elif action.action_type == "end_conversation":
                        state.conversation_active = False
                state.mark(Phase.EVALUATING)
                self._checkpoint(state)
                report = self.evaluator.evaluate(
                    state.session_id,
                    session_state={
                        "session_id": state.session_id, "status": "active",
                        "touch_budget": state.touch_budget, "touches_used": state.touches_used,
                    },
                )
                self.evaluator.save(report)
                state.last_evaluation = report.orchestrator_feedback.model_dump(mode="json")
                control = report.orchestrator_feedback.control
                if control == "stop":
                    # A live conversation may continue for its remaining turns
                    # even after the session has no contacts left.
                    if not (state.conversation_active and state.remaining_turns > 0
                            and state.remaining_touches == 0):
                        state.mark(Phase.STOPPED, error=report.orchestrator_feedback.reason)
                        break
                if control == "review":
                    state.mark(Phase.NEEDS_REVIEW, error=report.orchestrator_feedback.reason)
                    break
            except Exception as exc:
                state.mark(Phase.FAILED, error=f"{type(exc).__name__}: {exc}")
                break
            finally:
                self._checkpoint(state)
        else:
            state.mark(Phase.STOPPED, error="Maximum number of QA steps reached")
        self._checkpoint(state)
        return state


async def _demo() -> None:
    planner = ScriptedTestPlanner([{
        "action_type": "send_message", "target_id": "test_persona", "channel": "email",
        "sender": {"name": "QA Tester", "address": "tester@example.org"},
        "subject": "Offline QA test", "body": "Hello, this is a test of the message interface.",
    }])
    result = await Orchestrator(planner).run(RunState(session_id=f"qa_{uuid4().hex[:8]}"))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(_demo())
