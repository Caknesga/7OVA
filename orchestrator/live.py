"""Opt-in LIVE practice-session QA runner for the revel8 simulated Target API.

This is not an autonomous social-engineering controller. The planner may propose
only transparent, fixed QA messages. All transmission is restricted to the
simulator's `practice` scenario and requires an explicit caller opt-in.

The existing `orchestrator.controller.Orchestrator` remains offline-only.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from communicate.agent import CommunicationAgent
from communicate.client import TargetAPIClient
from communicate.schemas import ExecutionContext, PlannedAction
from evaluation.agent import EvaluationAgent
from planning.agent import PlannerInput

from .state import Phase, RunState


QA_SUBJECT = "Communication interface QA test"
QA_EMAIL = (
    "Hello, this is a communication interface test in the simulated "
    "environment. Can you confirm receipt?"
)
QA_TURN = "Hello, this is a simulated communication test. Is this message visible?"
ALLOWED_CHANNELS = frozenset({"email", "live_chat"})


class UnsafeActionError(ValueError):
    """Planner proposed an action outside transparent QA scope."""


def validate_qa_action(action: PlannedAction, state: RunState) -> None:
    """Fail closed on arbitrary LLM content, links, attachments, or impersonation."""
    if action.attachments:
        raise UnsafeActionError("QA runner does not permit attachments")
    if action.sender and (action.sender.name != "QA Tester" or action.sender.address):
        raise UnsafeActionError("QA sender must be named 'QA Tester' with no address")
    if action.action_type == "send_message":
        if not (action.target_id == state.target_id and action.channel == "email"
                and action.subject == QA_SUBJECT and action.body == QA_EMAIL):
            raise UnsafeActionError("Only the fixed QA receipt email is permitted")
    elif action.action_type == "start_conversation":
        if action.target_id != state.target_id or action.channel != "live_chat":
            raise UnsafeActionError("Only the fixed QA live_chat channel is permitted")
    elif action.action_type == "send_turn":
        if action.say != QA_TURN:
            raise UnsafeActionError("Only the fixed QA chat utterance is permitted")
    elif action.action_type != "end_conversation":
        raise UnsafeActionError("Unsupported action type")
    if action.action_type in {"send_message", "start_conversation"}:
        if state.remaining_touches <= 0:
            raise UnsafeActionError("No remaining touches")
    if action.action_type in {"send_turn", "end_conversation"}:
        if not state.conversation_active:
            raise UnsafeActionError("No active conversation")
        if action.action_type == "send_turn" and state.remaining_turns <= 0:
            raise UnsafeActionError("No remaining conversation turns")


class PracticeQAOrchestrator:
    """Coordinate planning, simulator contacts, logging, and read-only evaluation.

    `prepare()` creates a *practice* session, not an evaluation/scoring session.
    `run()` never sends arbitrary model output; only fixed QA communications.
    """

    def __init__(
        self,
        *,
        client: TargetAPIClient,
        planner: Any,
        planner_inputs: PlannerInput | dict[str, Any],
        log_dir: str | Path = "logs",
        state_dir: str | Path = "orchestrator/state",
        evaluation_dir: str | Path = "evaluation/results",
        max_steps: int = 3,
    ) -> None:
        if not 1 <= max_steps <= 10:
            raise ValueError("Live QA max_steps must be between 1 and 10")
        self.client = client
        self.planner = planner
        self.planner_inputs = PlannerInput.model_validate(planner_inputs).model_dump(mode="json")
        self.communication = CommunicationAgent(client)
        self.evaluator = EvaluationAgent(log_dir=log_dir, output_dir=evaluation_dir)
        self.log_dir = Path(log_dir)
        self.state_dir = Path(state_dir)
        self.max_steps = max_steps

    async def prepare(self, target_id: str, *, existing_session_id: str | None = None) -> RunState:
        """Discover the practice target and create/resume a session.

        Resume requires a local checkpoint so unknown contacts cannot silently
        be attributed to this run. A pending request stops for manual review.
        """
        scenarios = await self.client.get_scenarios()
        scenario = next((s for s in scenarios if s.get("id") == "practice"), None)
        if not scenario or scenario.get("open") is not True:
            raise ValueError("The practice scenario is unavailable")
        targets = {t["id"]: t for t in scenario.get("targets", [])}
        if target_id not in targets:
            raise ValueError(f"Target {target_id!r} is not in the practice scenario")

        if self.planner_inputs["research_report"]["target"]["id"] != target_id:
            raise ValueError("Research report target ID differs from chosen target")

        channels_response = await self.client.get_channels()
        channel_list = (channels_response.get("channels") if isinstance(channels_response, dict)
                        else channels_response)
        if not isinstance(channel_list, list):
            raise ValueError("Unexpected channel metadata")
        actual_channels = {item.get("id") for item in channel_list if isinstance(item, dict)}
        channels = sorted(ALLOWED_CHANNELS & actual_channels)
        if not channels:
            raise ValueError("The simulator has no supported QA channels")

        if existing_session_id:
            checkpoint_path = self.state_dir / f"{existing_session_id}.json"
            if not checkpoint_path.is_file():
                raise ValueError("No checkpoint found for this session; cannot safely resume")
            state = RunState.model_validate_json(checkpoint_path.read_text(encoding="utf-8"))
            if state.target_id != target_id or state.scenario_id != "practice":
                raise ValueError("Checkpoint target or scenario does not match")
        else:
            response = await self.client.create_session("practice")
            state = RunState(
                session_id=response["session_id"], scenario_id="practice", target_id=target_id,
                touch_budget=response["touch_budget"],
                touches_used=response.get("touches_used", 0),
                allowed_channels=channels,
            )

        remote = await self.client.get_session(state.session_id)
        if remote.get("scenario_id") != "practice":
            raise ValueError("The session does not belong to the practice scenario")
        self._synchronize(state, remote, allow_initial=not existing_session_id)
        state.allowed_channels = channels
        if not existing_session_id:
            self._checkpoint(state)
        return state

    def _synchronize(self, state: RunState, remote: dict[str, Any], *, allow_initial: bool = False) -> None:
        if remote.get("session_id") not in (None, state.session_id):
            raise ValueError("Session ID changed unexpectedly")
        used = int(remote["touches_used"])
        budget = int(remote["touch_budget"])
        if budget < 0 or used < 0 or used > budget:
            raise ValueError("Invalid remote touch budget")
        # If there are unexplained contacts, do not continue on an inconsistent log.
        if not allow_initial and used != state.touches_used:
            raise ValueError("Remote contact count differs from checkpoint; review before retrying")
        state.touch_budget = budget
        state.touches_used = used
        state.session_status = str(remote.get("status") or "unknown")

    def _checkpoint(self, state: RunState) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        dest = self.state_dir / f"{state.session_id}.json"
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, dest)
        return dest

    def _log(self, state: RunState, action: PlannedAction, context: ExecutionContext,
             result: Any, planner_decision: dict[str, Any] | None) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": state.session_id,
            "planned_action": action.model_dump(mode="json", exclude_none=True),
            "execution_context": context.model_dump(mode="json", exclude_none=True),
            "communication_result": result.model_dump(mode="json"),
            "planner_decision": planner_decision,
        }
        with (self.log_dir / f"{state.session_id}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _planner_input(self, state: RunState) -> PlannerInput:
        data = deepcopy(self.planner_inputs)
        data["simulator_environment"].update({
            "session_id": state.session_id,
            "target_id": state.target_id,
            "allowed_channels": state.allowed_channels,
            "remaining_touch_budget": state.remaining_touches,
            "conversation_active": state.conversation_active,
            "conversation_id": state.conversation_id if state.conversation_active else None,
            "remaining_conversation_turns": state.remaining_turns,
        })
        data["operational_constraints"]["actions_used"] = state.steps_completed
        entries: list[dict[str, Any]] = []
        log_path = self.log_dir / f"{state.session_id}.jsonl"
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entries.append(json.loads(line))
        replies: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for entry in entries:
            result = entry.get("communication_result") or {}
            if result.get("persona_says"):
                replies.append({"conversation_id": result.get("conversation_id"),
                                "persona_says": result["persona_says"]})
            for event in result.get("events") or []:
                if isinstance(event, dict):
                    events.append(event)
                    if event.get("type") == "reply":
                        replies.append(event)
        data["interaction_history"] = {
            "previous_actions": [e["planned_action"] for e in entries],
            "simulator_replies": replies,
            "observable_events": events,
            "last_evaluation": state.last_evaluation,
        }
        return PlannerInput.model_validate(data)

    async def _cleanup_conversation(self, state: RunState) -> None:
        """Close known active QA chat on normal workflow exit.

        Never proceed if the most recent request has an ambiguous outcome.
        """
        if (not state.conversation_active or not state.conversation_id
                or state.pending_request_id is not None
                or state.session_status != "active"):
            return
        action = PlannedAction(action_type="end_conversation")
        validate_qa_action(action, state)
        context = ExecutionContext(
            session_id=state.session_id,
            conversation_id=state.conversation_id,
            request_id=str(uuid4()),
            remaining_touches=state.remaining_touches,
            allowed_channels=state.allowed_channels,
            remaining_conversation_turns=state.remaining_turns,
        )
        result = await self.communication.execute(action, context)
        self._log(state, action, context, result, {"decision": "cleanup"})
        if not result.success:
            state.mark(Phase.NEEDS_REVIEW, error=f"Conversation cleanup failed: {result.error}")
            return
        state.conversation_active = False
        state.conversation_id = None
        state.turns_used = 0
        latest = await self.client.get_session(state.session_id)
        self._synchronize(state, latest, allow_initial=True)
        report = await self.evaluator.evaluate_from_api(state.session_id, self.client)
        self.evaluator.save(report)
        state.last_evaluation = report.orchestrator_feedback.model_dump(mode="json")

    async def run(self, state: RunState) -> RunState:
        """Run a bounded practice-only QA workflow using actual Target API methods."""
        if state.scenario_id != "practice":
            raise ValueError("Live QA runner supports practice scenario only")
        if state.pending_request_id is not None:
            state.mark(Phase.NEEDS_REVIEW, error="Pending action may have executed; reconcile manually")
            self._checkpoint(state)
            return state
        if state.phase in {Phase.COMPLETED, Phase.NEEDS_REVIEW, Phase.FAILED}:
            return state

        for _ in range(self.max_steps):
            try:
                remote = await self.client.get_session(state.session_id)
                self._synchronize(state, remote)
                if state.session_status != "active":
                    state.mark(Phase.STOPPED, error=f"Session status: {state.session_status}")
                    break
                if state.remaining_touches == 0 and not state.conversation_active:
                    state.mark(Phase.STOPPED, error="No remaining contacts")
                    break
                state.mark(Phase.PLANNING)
                self._checkpoint(state)

                if callable(getattr(self.planner, "plan", None)):
                    proposed = await self.planner.plan(self._planner_input(state))
                    state.last_planner_decision = proposed.model_dump(mode="json")
                    if proposed.decision == "stop":
                        state.mark(Phase.COMPLETED)
                        break
                    if proposed.decision == "insufficient_information":
                        state.mark(Phase.NEEDS_REVIEW, error=proposed.reasoning_summary)
                        break
                    action = PlannedAction.model_validate(proposed.planned_action)
                else:
                    action = await self.planner.propose(state)
                    if action is None:
                        state.mark(Phase.COMPLETED)
                        break
                    action = PlannedAction.model_validate(action)
                    state.last_planner_decision = {"decision": "execute", "planned_action": action.model_dump(mode="json")}

                validate_qa_action(action, state)
                request_id = str(uuid4())
                context = ExecutionContext(
                    session_id=state.session_id,
                    conversation_id=state.conversation_id,
                    request_id=request_id,
                    remaining_touches=state.remaining_touches,
                    allowed_channels=state.allowed_channels,
                    remaining_conversation_turns=(state.remaining_turns if state.conversation_active else None),
                )
                state.pending_request_id = request_id
                state.pending_action = action.model_dump(mode="json", exclude_none=True)
                state.mark(Phase.EXECUTING)
                self._checkpoint(state)  # durable record BEFORE any contact

                result = await self.communication.execute(action, context)
                self._log(state, action, context, result, state.last_planner_decision)
                state.steps_completed += 1
                # A failed/uncertain call is never retried with a new request ID.
                if not result.success:
                    state.mark(Phase.NEEDS_REVIEW, error=result.error or "Unknown transport failure")
                    break
                state.pending_action = None
                state.pending_request_id = None

                if result.touches_used is not None:
                    state.touches_used = result.touches_used
                if action.action_type == "start_conversation":
                    if not result.conversation_id:
                        raise ValueError("Missing conversation ID on live chat start")
                    state.conversation_id = result.conversation_id
                    state.conversation_active = True
                    state.turns_used = 0
                elif action.action_type == "send_turn":
                    state.turns_used = (result.turns_used if result.turns_used is not None
                                        else state.turns_used + 1)
                    if result.ended:
                        state.conversation_active = False
                elif action.action_type == "end_conversation":
                    state.conversation_active = False

                # Re-read authoritative status and events; this consumes no touch.
                latest = await self.client.get_session(state.session_id)
                self._synchronize(state, latest, allow_initial=True)
                state.mark(Phase.EVALUATING)
                self._checkpoint(state)
                report = await self.evaluator.evaluate_from_api(state.session_id, self.client)
                self.evaluator.save(report)
                state.last_evaluation = report.orchestrator_feedback.model_dump(mode="json")
                if report.orchestrator_feedback.control == "review":
                    state.mark(Phase.NEEDS_REVIEW, error=report.orchestrator_feedback.reason)
                    break
                if report.orchestrator_feedback.control == "stop":
                    if not (state.conversation_active and state.remaining_turns > 0
                            and state.remaining_touches == 0):
                        state.mark(Phase.STOPPED, error=report.orchestrator_feedback.reason)
                        break
            except (UnsafeActionError, ValidationError, ValueError) as exc:
                state.mark(Phase.NEEDS_REVIEW, error=f"{type(exc).__name__}: {exc}")
                break
            except Exception as exc:
                # Fail closed. If the crash happened during a request, pending
                # action stays in the checkpoint for reconciliation.
                state.mark(Phase.FAILED, error=f"{type(exc).__name__}: {exc}")
                break
            finally:
                self._checkpoint(state)
        else:
            state.mark(Phase.STOPPED, error="Configured QA step limit reached")
        # Only clean up a known active chat after the bounded QA loop.
        # Do not override a pending uncertain API request.
        try:
            await self._cleanup_conversation(state)
        except Exception as exc:
            state.mark(Phase.NEEDS_REVIEW, error=f"Cleanup error: {type(exc).__name__}: {exc}")
        self._checkpoint(state)
        return state
