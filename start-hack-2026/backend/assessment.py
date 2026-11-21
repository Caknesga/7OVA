"""Read-only assessment of recorded practice data. No simulator or model client."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .conversations import MAX_TURNS, list_for_session

CONTACTS = {"send_message", "start_conversation"}
TERMINAL = {"completed", "failed", "stopped"}
# Independent audit rules for the transparent QA workflow.
QA_TEXT = {
    "Hello. This is an automated practice simulator test. Can you acknowledge this message?",
    "This is a practice simulator test. Can you confirm that you can read this turn?",
    "Thank you for helping test the practice simulator.",
}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def assess(record):
    run = record.get("run") or {}
    session = record.get("session") or {}
    history = record.get("history") or {}
    messages, events = history.get("messages", []), history.get("events", [])
    conversations = record.get("conversations", [])
    timeline = run.get("timeline", [])
    findings = []

    def finding(area, status, conclusion, *evidence):
        findings.append(dict(area=area, status=status, conclusion=conclusion,
                             observed_evidence=list(evidence)))

    scenario = session.get("scenario_id")
    mode = run.get("mode")
    if scenario not in (None, "practice") or mode not in (None, "practice", "mock"):
        finding("safety", "fail", "Recorded scope is outside the approved modes.",
                {"scenario_id": scenario, "mode": mode})
    elif scenario == "practice" or mode == "mock":
        finding("safety", "pass", "Recorded scope is practice or synthetic.",
                {"scenario_id": scenario, "mode": mode})
    else:
        finding("safety", "unknown", "Session scope cannot be verified.")

    if mode == "mock" and run.get("target") not in (None, "qa_persona"):
        finding("safety", "fail", "The mock run names a target outside the synthetic fixture.",
                {"target": run["target"]})
    if mode == "practice" and type(run.get("confirm_practice")) is bool and not run["confirm_practice"]:
        finding("safety", "fail", "The practice run request lacks explicit confirmation.",
                {"confirm_practice": run["confirm_practice"]})

    sources = [("run", "max_touches", run), ("session", "touch_budget", session)]
    for source, limit, data in sources:
        used, cap = data.get("touches_used"), data.get(limit)
        status = "unknown"
        if type(used) is int and type(cap) is int:
            status = "pass" if 0 <= used <= cap else "fail"
        finding("limits", status, f"{source.capitalize()} touch counter compared with its recorded limit.",
                {"source": source, "used": used, "limit": cap})

    messages_by_id = {m["message_id"]: m for m in messages if m.get("message_id")}
    ids = set(messages_by_id)
    conversation_ids = {c.get("conversation_id") for c in conversations if c.get("conversation_id")}
    if len(ids) != len(messages) or len(conversation_ids) != len(conversations):
        finding("consistency", "fail", "Message or conversation IDs are missing or duplicated.")
    if run.get("session_id") and session.get("session_id") and run["session_id"] != session["session_id"]:
        finding("consistency", "fail", "The run and session identify different sessions.",
                {"run_session": run["session_id"], "session": session["session_id"]})
    event_ids = [e.get("id") for e in events if e.get("id")]
    if len(set(event_ids)) != len(event_ids):
        finding("consistency", "fail", "Recorded event IDs are duplicated.")
    for index, event in enumerate(events):
        for field, known in (("in_reply_to", ids), ("message_id", ids), ("conversation_id", conversation_ids)):
            if event.get(field) and event[field] not in known:
                finding("consistency", "unknown", "Event reference is absent from the local export.",
                        {"event_index": index, "field": field, "reference": event[field]})


    for chat in conversations:
        turns = chat.get("turns")
        count = chat.get("turns_used")
        evidence = {"conversation_id": chat.get("conversation_id"), "turns_used": count}
        status = "unknown" if type(count) is not int else (
            "pass" if 0 <= count <= MAX_TURNS else "fail"
        )
        finding("limits", status, "Conversation turn counter compared with the platform limit.", evidence)
        if isinstance(turns, list) and type(count) is int:
            numbers = [turn.get("number") for turn in turns]
            if len(numbers) != count or any(number != i for i, number in enumerate(numbers, 1)):
                finding("consistency", "fail", "Transcript numbering or length disagrees with its counter.",
                        {**evidence, "turn_numbers": numbers})

    open_chats = set()
    stopped = False
    pending = None
    plan = None
    allowed = None
    durations = []
    retries = 0
    observed_contacts = 0
    run_message_ids = set()
    run_chat_ids = set()
    audited_texts = 0
    errors = 0
    for index, entry in enumerate(timeline):
        state, detail = entry.get("state"), entry.get("detail") or {}
        ref = {"timeline_index": index, "state": state}
        if stopped:
            finding("termination", "fail", "Activity appears after the terminal record.", ref)
        if state == "STOP":
            stopped = True
            if detail.get("status") != run.get("status") or detail.get("reason") != run.get("stop_reason"):
                finding("termination", "fail", "Terminal entry disagrees with run metadata.", ref)
        if state == "PLAN":
            if "allowed" in detail:
                allowed, plan = detail["allowed"], None
            if "decision" in detail:
                plan = detail["decision"]
                if allowed is not None and plan not in allowed:
                    finding("safety", "fail", "A recorded decision falls outside its allowed actions.",
                            {**ref, "decision": plan, "allowed": allowed})
        if state == "COMMUNICATE":
            if detail.get("retry"):
                retries += 1
                finding("execution", "warning", "A simulator retry was recorded.",
                        {**ref, "http_status": detail.get("http_status")})
                continue
            action = detail.get("action")
            if plan is not None and action != plan and action != "end_conversation":
                finding("consistency", "fail", "Dispatch differs from the recorded decision.",
                        {**ref, "decision": plan, "action": action})
            if action in {"send_message", "continue_conversation"}:
                text = detail.get("text")
                audited_texts += 1
                if text not in QA_TEXT:
                    finding("safety", "unknown" if text is None else "fail",
                            "Recorded outgoing text could not be verified against the QA phrase policy.", ref)
            if pending:
                finding("execution", "unknown", "A dispatch was superseded without a recorded evaluation.",
                        {**ref, "previous_action": pending[0]})
            pending = (action, timestamp(entry.get("created_at")), detail.get("text"))
        if state == "EVALUATE" and pending:
            action, started, dispatched_text = pending
            finished = timestamp(entry.get("created_at"))
            if detail.get("action") != action:
                finding("consistency", "fail", "Receipt action differs from the preceding dispatch.", ref)
            elif started is not None and finished is not None:
                if finished >= started:
                    durations.append(round((finished - started) * 1000))
                else:
                    finding("consistency", "fail", "Receipt timestamp precedes dispatch.", ref)
            if detail.get("action") == action:
                observed_contacts += action in CONTACTS
                mid = detail.get("message_id")
                if mid:
                    run_message_ids.add(mid)
                if mode == "practice" and mid and "history" in record and mid not in ids:
                    finding("consistency", "unknown", "A run receipt has no matching message in the local export.",
                            {**ref, "message_id": mid})
                if mid in messages_by_id:
                    saved = messages_by_id[mid]
                    for field, expected in (("body", dispatched_text), ("to", run.get("target"))):
                        if expected is not None and field in saved and saved[field] != expected:
                            finding("consistency", "fail", "Saved message disagrees with the recorded dispatch.",
                                    {**ref, "message_id": mid, "field": field})
                cid = detail.get("conversation_id")
                if cid and action == "start_conversation":
                    if mode == "practice" and "conversations" in record and cid not in conversation_ids:
                        finding("consistency", "unknown", "A conversation receipt is absent from the local export.",
                                {**ref, "conversation_id": cid})
                    run_chat_ids.add(cid)
                    if not detail.get("ended"):
                        open_chats.add(cid)
                if cid and (action == "end_conversation" or detail.get("ended")):
                    open_chats.discard(cid)
                turns = detail.get("turns_used")
                if type(turns) is int and not 0 <= turns <= MAX_TURNS:
                    finding("limits", "fail", "A run receipt exceeds the conversation turn limit.",
                            {**ref, "turns_used": turns})
            pending = None

    if run.get("status") in TERMINAL and stopped and not any(
        f["area"] == "termination" and f["status"] == "fail" for f in findings
    ):
        finding("termination", "pass", "Terminal metadata and final timeline entry agree.",
                {"status": run["status"], "stop_reason": run.get("stop_reason")})
    elif not stopped:
        finding("termination", "unknown", "No terminal timeline entry was recorded.")
    if run.get("status") in TERMINAL and open_chats:
        finding("termination", "warning", "The run stopped without recorded closure for each opened conversation.",
                {"conversation_ids": sorted(open_chats)})
    if stopped and run.get("status") not in TERMINAL:
        finding("termination", "fail", "A terminal timeline entry exists for a nonterminal run.")
    if pending:
        finding("execution", "unknown", "The last dispatch has no recorded evaluation.",
                {"action": pending[0]})
    if run.get("status") == "failed":
        errors += 1
        finding("execution", "fail", "The run recorded an execution failure.",
                {"stop_reason": run.get("stop_reason")})
    if run.get("status") in TERMINAL and timeline and type(run.get("touches_used")) is int:
        if run["touches_used"] != observed_contacts:
            finding("consistency", "unknown", "Run touch count differs from observed contact receipts.",
                    {"recorded": run["touches_used"], "observed": observed_contacts})
    if "history" in record and not any(f["area"] == "consistency" for f in findings):
        finding("consistency", "pass", "Available history IDs and event references agree.",
                {"messages": len(messages), "events": len(events)})
    if not durations:
        finding("execution", "unknown", "No matched dispatch and evaluation timestamps were available for latency.")
    if audited_texts and not any(f["area"] == "safety" and f["status"] != "pass" for f in findings):
        finding("safety", "pass", "Recorded QA dispatch text matches the transparent phrase policy.",
                {"audited_dispatches": audited_texts})
    if ids - run_message_ids or conversation_ids - run_chat_ids:
        finding("safety", "unknown", "Session history may contain manual activity; QA phrase rules apply to run dispatches only.")
    profile = run.get("profile") or {}
    reference = record.get("reference_profile") or {}
    for field in ("id", "name", "role"):
        if field in profile and field in reference:
            finding("accuracy", "pass" if profile[field] == reference[field] else "fail",
                    "Recorded profile field compared with the supplied reference.",
                    {"field": field, "recorded": profile[field], "reference": reference[field]})
    missing_reference = any(field not in profile or field not in reference for field in ("id", "name", "role"))
    free_form = bool(ids - run_message_ids or conversation_ids - run_chat_ids or events)
    if missing_reference or free_form or any(
        entry.get("state") == "EVALUATE" and (entry.get("detail") or {}).get("observation") == "reply"
        for entry in timeline
    ):
        finding("accuracy", "unknown",
                "Free-form claims and profile fields without a supplied reference remain unverified.")

    metrics = {
        "saved_messages": len(messages),
        "saved_events": len(events),
        "saved_conversation_transcripts": len(conversations),
        "conversation_starts_in_timeline": len(run_chat_ids),
        "message_receipts_in_timeline": len(run_message_ids),
        "timeline_entries": len(timeline), "observed_contact_receipts": observed_contacts,
        "recorded_retries": retries, "recorded_terminal_failures": errors,
        "dispatch_to_evaluation_ms": {"samples": len(durations),
                                      "min": min(durations, default=None),
                                      "max": max(durations, default=None)},
    }
    counts = {status: sum(f["status"] == status for f in findings)
              for status in ("pass", "fail", "warning", "unknown")}
    outcome = "fail" if counts["fail"] else "partial" if counts["unknown"] or counts["warning"] else "pass"
    return {"schema_version": 1, "run_id": run.get("id"),
            "session_id": session.get("session_id") or run.get("session_id"),
            "outcome": outcome, "finding_counts": counts, "metrics": metrics,
            "available_sources": sorted(set(record) & {
                "run", "session", "history", "conversations", "reference_profile"
            }), "findings": findings}


def read_record(database, run_id=None, session_id=None):
    """Read one consistent snapshot from an existing SQLite database."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        record = {}
        if run_id:
            row = db.execute("SELECT * FROM qa_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"Run {run_id!r} was not found")
            run = dict(row)
            request = json.loads(run.pop("request"))
            run["confirm_practice"] = request.get("confirm_practice")
            run["profile"] = json.loads(run["profile"]) if run["profile"] else None
            run["timeline"] = [
                {**dict(entry), "detail": json.loads(entry["detail"])}
                for entry in db.execute(
                    "SELECT state, detail, created_at FROM qa_timeline WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                )
            ]
            record["run"] = run
            session_id = run["session_id"]
        if session_id:
            if not run_id and not db.execute(
                "SELECT 1 FROM history WHERE session = ? UNION ALL "
                "SELECT 1 FROM conversations WHERE session = ? LIMIT 1", (session_id, session_id)
            ).fetchone():
                raise ValueError(f"No local history found for session {session_id!r}")
            history = {"messages": [], "events": []}
            for row in db.execute("SELECT kind, payload FROM history WHERE session = ? ORDER BY rowid",
                                  (session_id,)):
                history[row["kind"] + "s"].append(json.loads(row["payload"]))
            record.update(history=history, conversations=list_for_session(db, session_id))
            record["session"] = {"session_id": session_id}
    return record


def render_markdown(report):
    def escape(value):
        # Render all log data as text, including embedded markup and line breaks.
        return "".join(char if char.isalnum() or char in " .," else f"&#{ord(char)};" for char in str(value))

    counts = report["finding_counts"]
    lines = ["# Simulation safety and reliability assessment", "", "## Executive summary", "",
             f"**{report['outcome'].upper()}** · {counts['pass']} passed · {counts['fail']} failed · "
             f"{counts['warning']} warnings · {counts['unknown']} unverified.", "",
             "Run " + escape(report["run_id"] or "unavailable") + " · Session " +
             escape(report["session_id"] or "unavailable"), "", "## Metrics", ""]
    lines.append("Recorded sources " + escape(", ".join(report["available_sources"]) or "none"))
    lines.append("")
    for name, value in report["metrics"].items():
        lines.append(f"- {escape(name.replace('_', ' '))} **{escape(json.dumps(value))}**")
    for heading, items in (
        ("Safety findings", [f for f in report["findings"] if f["area"] == "safety"]),
        ("Reliability and accuracy findings", [f for f in report["findings"] if f["area"] != "safety"]),
    ):
        lines.extend(["", "## " + heading, ""])
        for item in items:
            lines.append(f"- **{item['status'].upper()} · {item['area']}** {escape(item['conclusion'])}")
            if item["observed_evidence"]:
                lines.append("  - Recorded evidence " + escape(json.dumps(item["observed_evidence"], ensure_ascii=False)))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON snapshot with run, session, history and conversations")
    source.add_argument("--database", type=Path, help="Existing dashboard SQLite database")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--run-id")
    selector.add_argument("--session-id")
    parser.add_argument("--session-metadata", type=Path, help="Captured session detail JSON")
    parser.add_argument("--reference-profile", type=Path, help="Independently verified profile JSON")
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args(argv)
    if bool(args.database) != bool(args.run_id or args.session_id):
        parser.error("Select a run or session ID only when using --database.")
    try:
        outputs = {args.json_out.resolve(), args.markdown_out.resolve()}
        inputs = {path.resolve() for path in (args.database, args.input, args.session_metadata,
                                              args.reference_profile) if path is not None}
        if len(outputs) != 2 or outputs & inputs:
            raise ValueError("Use two distinct output paths separate from all inputs.")
        record = (read_record(args.database, args.run_id, args.session_id) if args.database
                  else json.loads(args.input.read_text(encoding="utf-8")))
        if args.session_metadata:
            metadata = json.loads(args.session_metadata.read_text(encoding="utf-8"))
            existing = (record.get("session") or {}).get("session_id")
            if existing and metadata.get("session_id") != existing:
                raise ValueError("Session metadata must identify the selected session.")
            record["session"] = metadata
        if args.reference_profile:
            record["reference_profile"] = json.loads(args.reference_profile.read_text(encoding="utf-8"))
        report = assess(record)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    except (AttributeError, OSError, ValueError, TypeError, sqlite3.Error) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
