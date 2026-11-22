"""Deterministic, evidence-based evaluator for an authorized AI-twin simulator.

Reads the existing logs/<session_id>.jsonl files created by the communication
logger and the earlier logs/<session_id>_live.json test format. Can additionally
read the current session and event data via TargetAPIClient's GET methods.

Produces a JSON report for the orchestrator and a Markdown report for humans.
No LLM, real-world messaging, strategic targeting, or tactic generation.

From the repository root:
    python -m evaluation.agent --session s_4028bb0392
    python -m evaluation.agent --session s_4028bb0392 --fetch-api
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.schemas import (
    EvaluationMetrics,
    EvaluationReport,
    EventCounts,
    InteractionSummary,
    OrchestratorFeedback,
)

# The evaluator recognizes simulator event *types*, but never treats an event
# (especially a form submission) as permission to collect or expose its data.
_EVENT_KEYS = {
    "reply": "reply",
    "link.opened": "link_opened",
    "page.submitted": "page_submitted",
    "attachment.opened": "attachment_opened",
}
_CONTACT_ACTIONS = frozenset({"send_message", "start_conversation"})


class EvaluationAgent:
    def __init__(
        self,
        log_dir: str | Path = "logs",
        output_dir: str | Path = "evaluation/results",
    ) -> None:
        self.log_dir = Path(log_dir)
        self.output_dir = Path(output_dir)

    def evaluate(
        self,
        session_id: str,
        *,
        session_state: dict[str, Any] | None = None,
        simulator_events: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> EvaluationReport:
        """Evaluate recorded actions and observable simulator evidence.

        No network activity and no session touches. ``session_state`` and
        ``simulator_events`` can be retrieved using the existing TargetAPIClient.
        """
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise ValueError("Invalid session_id")
        records, sources, warnings = self._read_logs(session_id)
        timeline: list[InteractionSummary] = []
        local_events: list[dict[str, Any]] = []
        responses_with_observations = 0
        persona_utterances = 0
        action_counts: Counter[str] = Counter()
        errors = 0
        seen_request_ids: set[tuple[str, str]] = set()

        for entry in records:
            action = entry.get("planned_action")
            result = entry.get("communication_result")
            if not isinstance(action, dict) or not isinstance(result, dict):
                warnings.append("Skipped malformed log entry without planned_action / communication_result")
                continue
            kind = str(action.get("action_type") or result.get("action_type") or "unknown")
            context = entry.get("execution_context") or {}
            request_id = context.get("request_id") if isinstance(context, dict) else None
            if isinstance(request_id, str) and request_id:
                key = (kind, request_id)
                if key in seen_request_ids:
                    warnings.append(f"Repeated request_id for {kind}; duplicate log entry ignored")
                    continue
                seen_request_ids.add(key)

            raw_events = result.get("events") or []
            if not isinstance(raw_events, list):
                warnings.append("Skipped malformed events field; expected list")
                raw_events = []
            valid_events = [e for e in raw_events if isinstance(e, dict)]
            local_events.extend(valid_events)
            success = result.get("success") is True
            action_counts[("ok_" if success else "failed_") + kind] += 1
            if not success:
                errors += 1
            has_persona = bool(result.get("persona_says"))
            persona_utterances += int(has_persona)
            has_reply = any(e.get("type") == "reply" for e in valid_events)
            responses_with_observations += int(success and (has_persona or has_reply))
            outgoing_logged = (
                bool(action.get("body")) if kind == "send_message" else
                bool(action.get("say")) if kind == "send_turn" else
                isinstance(action.get("target_id"), str) and bool(action.get("target_id"))
                if kind == "start_conversation" else True
            )
            if not outgoing_logged:
                warnings.append(f"Step {len(timeline)+1}: outgoing message content missing")
            touches = result.get("touches_used")
            if not isinstance(touches, int) or isinstance(touches, bool) or touches < 0:
                touches = None
            timeline.append(InteractionSummary(
                step=len(timeline) + 1,
                action_type=kind,
                channel=action.get("channel") if isinstance(action.get("channel"), str) else None,
                success=success,
                outgoing_logged=outgoing_logged,
                message_id=result.get("message_id") if isinstance(result.get("message_id"), str) else None,
                conversation_id=result.get("conversation_id") if isinstance(result.get("conversation_id"), str) else None,
                event_types=[str(e.get("type", "unknown")) for e in valid_events],
                persona_response_observed=has_persona,
                touches_used=touches,
                error=str(result.get("error")) if result.get("error") else None,
            ))

        provided_events = self._normalize_events(simulator_events, warnings)
        events = self._unique_events(local_events + provided_events)
        count = EventCounts()
        for event in events:
            field = _EVENT_KEYS.get(event.get("type"), "other")
            setattr(count, field, getattr(count, field) + 1)

        used: int | None = None
        budget: int | None = None
        session_status: str | None = None
        if session_state is not None:
            if not isinstance(session_state, dict):
                raise TypeError("session_state must be a dict or None")
            if session_state.get("session_id") not in (None, session_id):
                raise ValueError("Session state belongs to a different session_id")
            used = self._nonnegative_int(session_state.get("touches_used"))
            budget = self._nonnegative_int(session_state.get("touch_budget"))
            session_status = str(session_state["status"]) if session_state.get("status") else None
        if used is None:
            # touches_used is cumulative per session, not per-call: use maximum.
            local_touch_values = [t.touches_used for t in timeline if t.touches_used is not None]
            used = max(local_touch_values, default=None)
        if budget is not None and used is not None and used > budget:
            warnings.append("Reported touch usage exceeds budget; verify session state")
        remaining = max(budget - used, 0) if budget is not None and used is not None else None
        total = len(timeline)
        ok = sum(t.success for t in timeline)
        failed = total - ok
        metrics = EvaluationMetrics(
            actions_logged=total,
            successful_actions=ok,
            failed_actions=failed,
            messages_sent=action_counts["ok_send_message"],
            conversations_started=action_counts["ok_start_conversation"],
            conversation_turns_sent=action_counts["ok_send_turn"],
            conversations_ended=action_counts["ok_end_conversation"],
            actions_with_observable_response=responses_with_observations,
            persona_utterances=persona_utterances,
            events=count,
            touch_budget=budget,
            touches_used=used,
            remaining_touches=remaining,
            transport_success_rate=(ok / total) if total else None,
        )

        conversation_active: bool | None = None
        if timeline:
            last_live = next((t for t in reversed(timeline) if t.action_type in
                {"start_conversation", "send_turn", "end_conversation"} and t.success), None)
            if last_live is not None:
                conversation_active = last_live.action_type != "end_conversation"
                if last_live.action_type == "send_turn":
                    # Ended=true can be available in stored raw result; inspect last corresponding record.
                    turn_results = [r.get("communication_result", {}) for r in records
                        if isinstance(r.get("communication_result"), dict)
                        and (r.get("planned_action") or {}).get("action_type") == "send_turn"]
                    if turn_results and turn_results[-1].get("ended") is True:
                        conversation_active = False
        if session_status is not None and session_status != "active":
            feedback = OrchestratorFeedback(
                control="stop", reason=f"Simulator session is {session_status}",
                session_status=session_status, conversation_active=False)
        elif remaining == 0:
            feedback = OrchestratorFeedback(
                control="stop", reason="No remaining session contacts",
                session_status=session_status, conversation_active=conversation_active)
        elif not timeline:
            feedback = OrchestratorFeedback(
                control="review", reason="No outgoing interaction log was available for evaluation",
                session_status=session_status, conversation_active=conversation_active)
        elif count.page_submitted:
            feedback = OrchestratorFeedback(
                control="review", reason="Sensitive form-submission event recorded; require review",
                session_status=session_status, conversation_active=conversation_active)
        elif failed or warnings:
            feedback = OrchestratorFeedback(
                control="review", reason="Execution errors or incomplete records require review",
                session_status=session_status, conversation_active=conversation_active)
        else:
            feedback = OrchestratorFeedback(
                control="decision_required",
                reason="Technical evaluation complete; orchestrator owns any next decision",
                session_status=session_status, conversation_active=conversation_active)

        limitations = [
            "No hidden target intent, suspicion or susceptibility is available from the API.",
            "Response and click observations do not establish factual accuracy or consent.",
            "Official jury scores for targeting, appearance, architecture and pitch cannot be inferred.",
            "No generated communication strategy or sensitive response content is included.",
        ]
        if not sources:
            warnings.append("No local action logs found; simulator events alone cannot reconstruct outgoing content")
        if simulator_events is None:
            limitations.append("No fresh GET /sessions/{id}/events evidence was supplied.")
        if session_state is None:
            limitations.append("No current GET /sessions/{id} state was supplied; budget/status may be unknown.")

        status = "no_data" if not timeline and not events else (
            "partial" if not timeline or warnings or session_state is None else "completed"
        )
        return EvaluationReport(
            session_id=session_id,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            evaluation_status=status,
            metrics=metrics,
            timeline=timeline,
            orchestrator_feedback=feedback,
            warnings=list(dict.fromkeys(warnings)),
            limitations=limitations,
            source_files=sources,
        )

    async def evaluate_from_api(self, session_id: str, client: Any) -> EvaluationReport:
        """Read-only API enrichment. No simulated contact and no new session."""
        session = await client.get_session(session_id)
        events = await client.get_events(session_id)
        return self.evaluate(session_id, session_state=session, simulator_events=events)

    def save(self, report: EvaluationReport) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / f"{report.session_id}_evaluation.json"
        md_path = self.output_dir / f"{report.session_id}_evaluation.md"
        json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        md_path.write_text(self.to_markdown(report), encoding="utf-8")
        return json_path, md_path

    @staticmethod
    def to_markdown(report: EvaluationReport) -> str:
        m = report.metrics
        fraction = f"{100*m.transport_success_rate:.1f}%" if m.transport_success_rate is not None else "N/A"
        lines = [
            f"# Communication evaluation — {report.session_id}", "",
            f"- Evaluated at (UTC): {report.evaluated_at}",
            f"- Evidence status: **{report.evaluation_status}**",
            f"- Successful API operations: **{m.successful_actions}/{m.actions_logged}** ({fraction})",
            f"- Contacts sent: **{m.messages_sent + m.conversations_started}**",
            f"- Live turns sent: **{m.conversation_turns_sent}**",
            f"- Operations with an observed reply or utterance: **{m.actions_with_observable_response}**",
            f"- Touches used / budget: **{m.touches_used if m.touches_used is not None else '?'} / "
            f"{m.touch_budget if m.touch_budget is not None else '?'}**",
            f"- Remaining touches: **{m.remaining_touches if m.remaining_touches is not None else 'unknown'}**",
            "", "## Observed simulator events", "",
            f"- Reply events: {m.events.reply}",
            f"- Links opened: {m.events.link_opened}",
            f"- Form submissions: {m.events.page_submitted} (contents not recorded here)",
            f"- Attachments opened: {m.events.attachment_opened}",
            f"- Other event types: {m.events.other}",
            "", "## Execution timeline", "",
        ]
        if not report.timeline:
            lines.append("No outgoing action log available.")
        for t in report.timeline:
            state = "OK" if t.success else "FAILED"
            channel = f" on {t.channel}" if t.channel else ""
            observed = "response observed" if t.persona_response_observed or "reply" in t.event_types else "no response observed"
            lines.append(f"{t.step}. **{t.action_type}**{channel}: {state}; {observed}")
            if t.error:
                lines.append(f"   - Technical error: `{t.error.replace('`','')[:240]}`")
        lines.extend([
            "", "## Feedback for orchestrator", "",
            f"- Control: **{report.orchestrator_feedback.control}**",
            f"- Reason: {report.orchestrator_feedback.reason}",
            "", "## Warnings", "",
        ])
        lines.extend([f"- {w}" for w in report.warnings] or ["- None"])
        lines.extend(["", "## Interpretation limits", ""])
        lines.extend([f"- {x}" for x in report.limitations])
        lines.append("")
        return "\n".join(lines)

    def _read_logs(self, session_id: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        rows: list[dict[str, Any]] = []
        paths: list[str] = []
        warnings: list[str] = []
        jsonl = self.log_dir / f"{session_id}.jsonl"
        if jsonl.exists():
            paths.append(str(jsonl))
            for n, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError("Not a JSON object")
                    if obj.get("session_id") not in (None, session_id):
                        warnings.append(f"Ignored JSONL line {n} with another session ID")
                        continue
                    rows.append(obj)
                except (json.JSONDecodeError, ValueError) as exc:
                    warnings.append(f"Invalid JSONL line {n}: {exc}")
        live = self.log_dir / f"{session_id}_live.json"
        if live.exists():
            paths.append(str(live))
            try:
                obj = json.loads(live.read_text(encoding="utf-8"))
                if not isinstance(obj, dict) or obj.get("session_id") != session_id:
                    warnings.append("Ignored live log with mismatched session ID")
                else:
                    start = obj.get("initial_response")
                    turn = obj.get("turn_response")
                    if isinstance(start, dict):
                        rows.append({"planned_action": {"action_type": "start_conversation",
                            "target_id": "logged_in_simulator", "channel": obj.get("channel")},
                            "communication_result": start})
                    if isinstance(turn, dict):
                        rows.append({"planned_action": {"action_type": "send_turn",
                            "say": obj.get("sent_message")}, "communication_result": turn})
                    if jsonl.exists():
                        warnings.append("Mixed JSONL/live logs may not preserve exact chronology")
            except (json.JSONDecodeError, OSError) as exc:
                warnings.append(f"Invalid live log: {exc}")
        return rows, paths, warnings

    @staticmethod
    def _normalize_events(
        events: dict[str, Any] | list[dict[str, Any]] | None,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        if events is None:
            return []
        raw = events.get("events") if isinstance(events, dict) else events
        if not isinstance(raw, list):
            warnings.append("Simulator events response has no events list")
            return []
        return [e for e in raw if isinstance(e, dict)]

    @staticmethod
    def _unique_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for event in events:
            event_id = event.get("id")
            # Deduplicate by authoritative ID when available; otherwise retain
            # duplicates because distinct events may have identical payloads.
            if isinstance(event_id, str) and event_id:
                if event_id in seen:
                    continue
                seen.add(event_id)
            out.append(event)
        return out

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate recorded simulator communications")
    parser.add_argument("--session", required=True, help="Existing session ID")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--output-dir", default="evaluation/results")
    parser.add_argument("--fetch-api", action="store_true", help="Read session state/events through the simulator API")
    args = parser.parse_args()

    evaluator = EvaluationAgent(log_dir=args.log_dir, output_dir=args.output_dir)
    if args.fetch_api:
        # Supports either folder naming convention in the user's project.
        try:
            from communicate.client import TargetAPIClient
        except ImportError:
            print("Error: TargetAPIClient not found. Please install the communicate package.")
            return
        async with TargetAPIClient() as client:
            report = await evaluator.evaluate_from_api(args.session, client)
    else:
        report = evaluator.evaluate(args.session)
    json_path, md_path = evaluator.save(report)
    print(evaluator.to_markdown(report))
    print(f"\nSaved JSON: {json_path}\nSaved Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(_main())
