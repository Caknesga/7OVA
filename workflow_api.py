"""Read-only dashboard API backed by saved practice workflow records."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException


ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")
PHASE_STAGES = {
    "created": "START", "planning": "PLAN", "executing": "COMMUNICATE", "evaluating": "EVALUATE",
    "completed": "STOP", "needs_review": "STOP", "stopped": "STOP", "failed": "STOP",
}
ACTION_NAMES = {
    "send_message": "send_message", "start_conversation": "start_conversation",
    "send_turn": "continue_conversation", "end_conversation": "end_conversation",
}
EVENT_TYPES = {"reply", "link.opened", "page.submitted", "attachment.opened"}
STOP_REASONS = {
    "Configured QA step limit reached", "Maximum number of QA steps reached", "No remaining contacts",
    "Touch budget exhausted", "Conversation turn budget exhausted",
}


def object_fields(value):
    return value if isinstance(value, dict) else {}


def text_field(value):
    return value if isinstance(value, str) else None


def read_object(path):
    try:
        return object_fields(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


class PracticeRecords:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.checkpoints = self.root / "orchestrator/state"

    def file(self, directory, name):
        try:
            path = (self.root / directory / name).resolve()
        except (OSError, RuntimeError):
            return None
        return path if path.is_relative_to(self.root) else None

    def state(self, session_id):
        if not IDENTIFIER.fullmatch(session_id):
            return None
        path = self.file("orchestrator/state", f"{session_id}.json")
        data = read_object(path) if path else {}
        if (data.get("session_id") == session_id and data.get("scenario_id") == "practice"
                and text_field(data.get("phase")) in PHASE_STAGES):
            return data
        return None

    def states(self):
        states = [self.state(path.stem) for path in self.checkpoints.glob("*.json")]
        return sorted(
            (state for state in states if state),
            key=lambda state: (timestamp(state.get("updated_at")), state["session_id"]),
            reverse=True,
        )

    def communications(self, session_id):
        path = self.file("logs", f"{session_id}.jsonl")
        if path is None:
            return
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        entry = object_fields(json.loads(line))
                    except ValueError:
                        continue
                    if entry.get("session_id") == session_id:
                        yield entry
        except (OSError, UnicodeError):
            return

    def report(self, session_id):
        path = self.file("evaluation/results", f"{session_id}_evaluation.json")
        data = read_object(path) if path else {}
        return data if data.get("session_id") == session_id else {}


def run_summary(state):
    phase = state["phase"]
    stage = PHASE_STAGES[phase]
    summary = {
        "id": state["session_id"], "session_id": state["session_id"],
        "mode": "practice", "target": text_field(state.get("target_id")), "state": stage,
        "status": phase if stage == "STOP" else "running",
        "max_touches": state.get("touch_budget"), "touches_used": state.get("touches_used"),
        "updated_at": state.get("updated_at"),
    }
    reason = text_field(state.get("last_error"))
    if stage == "STOP" and reason in STOP_REASONS:
        summary["stop_reason"] = reason
    return summary


def run_timeline(records, state):
    session_id = state["session_id"]
    timeline = [{"state": "START", "detail": {}}]
    if state.get("research_report_path"):
        timeline.append({"state": "RESEARCH", "detail": {"research_reference": True}})

    recorded = []
    for entry in records.communications(session_id):
        action = object_fields(entry.get("planned_action"))
        result = object_fields(entry.get("communication_result"))
        name = ACTION_NAMES.get(text_field(action.get("action_type")))
        if not name or not result:
            continue
        detail = {"action": name, "channel": text_field(action.get("channel"))}
        date = entry.get("timestamp")
        if object_fields(entry.get("planner_decision")).get("decision") == "execute":
            recorded.append({"state": "PLAN", "created_at": date, "detail": {
                "decision": name, "channel": detail["channel"],
            }})
        recorded.append({"state": "COMMUNICATE", "created_at": date, "detail": {
            **detail, "success": result.get("success"),
        }})

    report = records.report(session_id)
    if report:
        metrics = object_fields(report.get("metrics"))
        recorded.append({"state": "EVALUATE", "created_at": report.get("evaluated_at"), "detail": {
            "evaluation_status": report.get("evaluation_status"),
            "actions_logged": metrics.get("actions_logged"),
        }})
    timeline.extend(sorted(recorded, key=lambda entry: timestamp(entry.get("created_at"))))
    decision = text_field(object_fields(state.get("last_planner_decision")).get("decision"))
    if decision in {"stop", "review"}:
        timeline.append({"state": "PLAN", "detail": {"decision": decision}})
    if PHASE_STAGES[state["phase"]] == "STOP":
        timeline.append({"state": "STOP", "created_at": state.get("updated_at"), "detail": {"status": state["phase"]}})
    return timeline


def session_activity(records, state):
    messages, events, conversations = {}, {}, {}
    for step, entry in enumerate(records.communications(state["session_id"]), 1):
        action = object_fields(entry.get("planned_action"))
        result = object_fields(entry.get("communication_result"))
        channel = text_field(action.get("channel"))
        message_id = text_field(result.get("message_id"))
        if message_id:
            messages[message_id] = {"message_id": message_id, "channel": channel, "to": state.get("target_id")}

        conversation_id = text_field(result.get("conversation_id"))
        if conversation_id:
            conversation = conversations.setdefault(conversation_id, {
                "conversation_id": conversation_id, "to": state.get("target_id"), "channel": None,
                "status": "active", "turns_used": 0, "max_turns": 10, "turns": [], "events": [],
            })
            if channel:
                conversation["channel"] = channel
            if result.get("conversation_status"):
                conversation["status"] = result["conversation_status"]
            if result.get("ended") or (action.get("action_type") == "end_conversation" and result.get("success")):
                conversation["status"] = "ended"
            if isinstance(result.get("turns_used"), int):
                conversation["turns_used"] = max(conversation["turns_used"], result["turns_used"])

        raw_events = result.get("events")
        for index, raw_event in enumerate(raw_events if isinstance(raw_events, list) else []):
            event = object_fields(raw_event)
            if not event:
                continue
            kind = text_field(event.get("type"))
            event_id = text_field(event.get("id")) or f"observed-{step}-{index}"
            observed = {
                "id": event_id, "type": kind if kind in EVENT_TYPES else "other",
                "channel": text_field(event.get("channel")) or channel,
                "message_id": text_field(event.get("message_id")) or message_id,
                "in_reply_to": text_field(event.get("in_reply_to")),
                "conversation_id": text_field(event.get("conversation_id")) or conversation_id,
            }
            events[event_id] = observed
    for event in events.values():
        conversation = conversations.get(event["conversation_id"])
        if conversation:
            conversation["events"].append(event)
    return {"messages": list(messages.values()), "events": list(events.values())}, list(conversations.values())


def create_app(root=ROOT):
    records = PracticeRecords(root)
    app = FastAPI(title="Practice Workflow Records")

    def state_for(session_id):
        state = records.state(session_id)
        if state is None:
            raise HTTPException(404, "Practice run not found.")
        return state

    @app.get("/api/qa/runs")
    def list_runs():
        return [run_summary(state) for state in records.states()]

    @app.get("/api/qa/runs/{session_id}")
    def get_run(session_id: str):
        state = state_for(session_id)
        return {**run_summary(state), "timeline": run_timeline(records, state)}

    @app.get("/api/scenarios")
    def list_scenarios():
        known_targets = {text_field(state.get("target_id")) for state in records.states()}
        targets = sorted(target for target in known_targets if target)
        return [{"id": "practice", "targets": [{"id": target} for target in targets]}]

    @app.get("/api/sessions")
    def list_sessions():
        return [{"session_id": state["session_id"], "status": state.get("session_status")}
                for state in records.states()]

    @app.get("/api/sessions/{session_id}/history")
    def get_history(session_id: str):
        history, _ = session_activity(records, state_for(session_id))
        return history

    @app.get("/api/sessions/{session_id}/conversations")
    def get_conversations(session_id: str):
        _, conversations = session_activity(records, state_for(session_id))
        return conversations

    return app


app = create_app()
