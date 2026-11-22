import json

from fastapi.testclient import TestClient

from workflow_api import create_app


def save(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def checkpoint(root, session_id, scenario_id="practice", **fields):
    save(root, f"orchestrator/state/{session_id}.json", {
        "session_id": session_id, "scenario_id": scenario_id, "phase": "stopped",
        "target_id": "practice-persona", "touch_budget": 30, "touches_used": 1,
        "session_status": "active", "updated_at": "2026-11-22T09:00:00Z", **fields,
    })


def test_lists_practice_checkpoints_in_order_and_reloads_updates(tmp_path):
    checkpoint(tmp_path, "practice-old")
    checkpoint(tmp_path, "offline", scenario_id="offline_qa")
    checkpoint(tmp_path, "other-scenario", scenario_id="eval")
    checkpoint(tmp_path, "practice-current", phase="executing", updated_at="2026-11-22T10:00:00Z")
    save(tmp_path, "orchestrator/state/invalid.json", {"phase": ["planning"]})

    with TestClient(create_app(tmp_path)) as client:
        runs = client.get("/api/qa/runs").json()
        assert [run["id"] for run in runs] == ["practice-current", "practice-old"]
        assert (runs[0]["status"], runs[0]["state"]) == ("running", "COMMUNICATE")
        assert client.get("/api/qa/runs/other-scenario").status_code == 404
        assert client.post("/api/qa/runs").status_code == 405
        checkpoint(tmp_path, "practice-current", phase="completed")
        refreshed = client.get("/api/qa/runs/practice-current").json()
        assert refreshed["status"] == "completed"
        assert [step["state"] for step in refreshed["timeline"]] == ["START", "STOP"]


def test_projects_existing_actions_and_events_without_private_payloads(tmp_path):
    session = "recorded"
    checkpoint(tmp_path, session, research_report_path="research/private.json")
    save(tmp_path, "logs/recorded.jsonl", {
        "session_id": session, "timestamp": "2026-11-22T08:40:00Z",
        "planned_action": {"action_type": "send_message", "channel": "email", "body": "private payload"},
        "planner_decision": {"decision": "execute", "reasoning_summary": "private reasoning"},
        "communication_result": {
            "success": True, "message_id": "m-1", "persona_says": "private reply",
            "events": [{"id": "e-1", "type": "reply", "in_reply_to": "m-1", "body": "private reply"}],
        },
    })
    with (tmp_path / "logs/recorded.jsonl").open("a", encoding="utf-8") as log:
        log.write('\n{"concurrent_save":')
    save(tmp_path, "evaluation/results/recorded_evaluation.json", {
        "session_id": session, "evaluated_at": "2026-11-22T08:45:00Z",
        "evaluation_status": "completed", "metrics": {"actions_logged": 1},
    })

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/qa/runs/recorded")
        steps = response.json()["timeline"]
        assert [step["state"] for step in steps] == ["START", "RESEARCH", "PLAN", "COMMUNICATE", "EVALUATE", "STOP"]
        assert steps[3]["detail"]["success"] is True
        assert steps[4]["detail"]["actions_logged"] == 1
        assert "private" not in response.text
        history = client.get("/api/sessions/recorded/history")
        assert history.json()["events"][0]["in_reply_to"] == "m-1"
        assert "private" not in history.text


def test_projects_live_conversation_metadata(tmp_path):
    checkpoint(tmp_path, "chat")
    save(tmp_path, "logs/chat.jsonl", {
        "session_id": "chat", "planned_action": {"action_type": "send_turn", "channel": "live_chat"},
        "communication_result": {
            "conversation_id": "c-1", "turns_used": 2, "ended": True, "persona_says": "private reply",
        },
    })
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/sessions/chat/conversations")
        conversation, = response.json()
        assert (conversation["conversation_id"], conversation["status"], conversation["turns_used"]) == ("c-1", "ended", 2)
        assert "private" not in response.text
