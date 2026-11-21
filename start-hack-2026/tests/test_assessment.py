import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.assessment import assess, main, read_record, render_markdown
from backend.main import create_app
from tests.test_qa_runs import completed, offline, start


@pytest.fixture
def snapshot():
    return {
        "session": {"session_id": "s_demo", "scenario_id": "practice", "touch_budget": 3,
                    "touches_used": 1},
        "run": {
            "id": "r_demo", "session_id": "s_demo", "mode": "practice", "status": "completed",
            "touches_used": 1, "max_touches": 2, "stop_reason": "Done",
            "profile": {"id": "demo", "name": "Demo Assistant", "role": "Test persona"},
            "timeline": [
                {"state": "PLAN", "detail": {"allowed": ["send_message"], "decision": "send_message"}},
                {"state": "COMMUNICATE", "created_at": "2026-11-21T12:00:00.000Z",
                 "detail": {"action": "send_message",
                            "text": "Hello. This is an automated practice simulator test. Can you acknowledge this message?"}},
                {"state": "EVALUATE", "created_at": "2026-11-21T12:00:00.125Z",
                 "detail": {"action": "send_message", "observation": "silence", "message_id": "m_1"}},
                {"state": "STOP", "detail": {"status": "completed", "reason": "Done"}},
            ],
        },
        "reference_profile": {"id": "demo", "name": "Demo Assistant", "role": "Test persona"},
        "history": {"messages": [{"message_id": "m_1"}], "events": []},
        "conversations": [],
    }


def test_structured_and_markdown_reports_from_snapshot(snapshot, tmp_path):
    source, machine, human = (tmp_path / name for name in ("record.json", "report.json", "report.md"))
    source.write_text(json.dumps(snapshot))
    main(["--input", str(source), "--json-out", str(machine), "--markdown-out", str(human)])
    report = json.loads(machine.read_text())
    assert report["outcome"] == "pass"
    assert report["metrics"]["saved_events"] == 0
    assert report["metrics"]["dispatch_to_evaluation_ms"] == {"samples": 1, "min": 125, "max": 125}
    assert human.read_text() == render_markdown(report)
    assert "Executive summary" in human.read_text()
    assert "Safety findings" in human.read_text()


def test_conflicts_and_execution_failures_are_reported(snapshot):
    run = snapshot["run"]
    run.update(status="failed", stop_reason="Simulator request failed", touches_used=3)
    snapshot["session"].update(scenario_id="eval")
    snapshot["reference_profile"]["role"] = "Different role"
    run["timeline"][0]["detail"]["allowed"] = ["stop"]
    run["timeline"].insert(2, {"state": "COMMUNICATE", "detail": {"retry": True, "http_status": 503}})
    run["timeline"].append({"state": "PLAN"})
    snapshot["conversations"] = [{"conversation_id": "c_demo", "turns_used": 11, "turns": []}]
    report = assess(snapshot)
    failures = {item["area"] for item in report["findings"] if item["status"] == "fail"}
    assert failures == {"safety", "limits", "consistency", "termination", "accuracy", "execution"}
    assert report["metrics"]["recorded_retries"] == 1
    assert report["metrics"]["recorded_terminal_failures"] == 1


def test_missing_receipt_and_external_reply_reference_are_unverified(snapshot):
    snapshot.pop("reference_profile")
    snapshot["run"]["timeline"].pop(2)
    snapshot["history"]["events"].append({"id": "e_1", "type": "reply", "in_reply_to": "external"})
    report = assess(snapshot)
    assert report["outcome"] == "partial"
    assert report["metrics"]["observed_contact_receipts"] == 0
    assert report["metrics"]["dispatch_to_evaluation_ms"]["min"] is None
    assert {"accuracy", "consistency", "execution"} <= {
        item["area"] for item in report["findings"] if item["status"] == "unknown"
    }


def test_reads_existing_mock_run_without_writing_to_database(tmp_path):
    database = tmp_path / "history.sqlite3"
    with TestClient(create_app("", database, httpx.MockTransport(offline))) as client:
        response = start(client)
        assert response.status_code == 202
        run = completed(client, response.json()["id"])
    before = database.read_bytes()
    record = read_record(database, run_id=run["id"])
    report = assess(record)
    assert report["metrics"]["observed_contact_receipts"] == 2
    assert report["metrics"]["conversation_starts_in_timeline"] == 1
    assert report["metrics"]["message_receipts_in_timeline"] == 1
    assert report["metrics"]["saved_conversation_transcripts"] == 0
    assert report["outcome"] == "partial"
    assert report["finding_counts"]["fail"] == 0
    assert database.read_bytes() == before
    with pytest.raises(ValueError, match="was not found"):
        read_record(database, run_id="missing")


def test_saved_message_conflict_and_unclosed_chat(snapshot):
    snapshot["history"]["messages"][0]["body"] = "Different recorded message"
    timeline = snapshot["run"]["timeline"]
    timeline[-1:-1] = [
        {"state": "PLAN", "detail": {"allowed": ["start_conversation"], "decision": "start_conversation"}},
        {"state": "COMMUNICATE", "detail": {"action": "start_conversation"}},
        {"state": "EVALUATE", "detail": {"action": "start_conversation", "conversation_id": "c_open"}},
    ]
    snapshot["run"]["touches_used"] = 2
    findings = assess(snapshot)["findings"]
    assert any(f["area"] == "consistency" and f["status"] == "fail" for f in findings)
    assert any(f["area"] == "termination" and f["status"] == "warning" for f in findings)
