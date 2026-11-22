"""Run from repository root: python -m pytest evaluation/test_agent.py -v"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.agent import EvaluationAgent


def _entry(kind, response, *, session_id="s_test", request_id="a1", **action):
    return {
        "session_id": session_id,
        "planned_action": {"action_type": kind, **action},
        "execution_context": {"session_id": session_id, "request_id": request_id},
        "communication_result": {"action_type": kind, "success": True, **response},
    }


def _write_jsonl(tmp_path: Path, *entries):
    (tmp_path / "s_test.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def test_empty_delivery_not_a_transport_failure(tmp_path):
    _write_jsonl(tmp_path, _entry("send_message", {
        "message_id": "m_1", "touches_used": 1, "events": []},
        target_id="robert", channel="email", body="Confirm receipt."))
    report = EvaluationAgent(tmp_path).evaluate("s_test", session_state={
        "session_id": "s_test", "status": "active", "touch_budget": 30,
        "touches_used": 1}, simulator_events={"events": []})
    assert report.metrics.successful_actions == 1
    assert report.metrics.actions_with_observable_response == 0
    assert report.metrics.transport_success_rate == 1.0
    assert report.metrics.remaining_touches == 29
    assert report.orchestrator_feedback.control == "decision_required"
    assert report.evaluation_status == "completed"


def test_reply_deduplicated_between_log_and_simulator(tmp_path):
    reply = {"id": "e_7", "type": "reply", "body": "Received."}
    _write_jsonl(tmp_path, _entry("send_message", {
        "message_id": "m_1", "touches_used": 1, "events": [reply]},
        target_id="robert", channel="email", body="Test."))
    report = EvaluationAgent(tmp_path).evaluate("s_test", simulator_events={"events": [reply]})
    assert report.metrics.events.reply == 1
    assert report.metrics.actions_with_observable_response == 1
    assert "Received." not in EvaluationAgent.to_markdown(report)


def test_live_legacy_log_with_persona_response(tmp_path):
    live = {
        "session_id": "s_test", "conversation_id": "c_1", "channel": "live_chat",
        "initial_response": {"action_type": "start_conversation", "success": True,
            "conversation_id": "c_1", "conversation_status": "answered", "persona_says": "Hello?", "touches_used": 1},
        "sent_message": "Please confirm receipt.",
        "turn_response": {"action_type": "send_turn", "success": True,
            "conversation_id": "c_1", "persona_says": "I received it.", "ended": False, "turns_used": 1},
    }
    (tmp_path / "s_test_live.json").write_text(json.dumps(live), encoding="utf-8")
    report = EvaluationAgent(tmp_path).evaluate("s_test")
    assert report.metrics.conversations_started == 1
    assert report.metrics.conversation_turns_sent == 1
    assert report.metrics.persona_utterances == 2
    assert report.orchestrator_feedback.conversation_active is True


def test_session_closed_forces_stop(tmp_path):
    _write_jsonl(tmp_path, _entry("send_message", {
        "message_id": "m_1", "touches_used": 1, "events": []},
        body="Test.", target_id="robert", channel="email"))
    report = EvaluationAgent(tmp_path).evaluate("s_test", session_state={
        "session_id": "s_test", "status": "ended", "touch_budget": 30, "touches_used": 1})
    assert report.orchestrator_feedback.control == "stop"


def test_failed_action_needs_review(tmp_path):
    _write_jsonl(tmp_path, _entry("send_message", {"success": False, "error": "HTTP 401", "events": []},
        body="Test.", target_id="robert", channel="email"))
    report = EvaluationAgent(tmp_path).evaluate("s_test")
    assert report.metrics.failed_actions == 1
    assert report.orchestrator_feedback.control == "review"


def test_sensitive_form_event_requires_review_without_values(tmp_path):
    _write_jsonl(tmp_path, _entry("send_message", {
        "message_id": "m_1", "touches_used": 1,
        "events": [{"id": "e_1", "type": "page.submitted", "fields": ["password"]}]},
        body="Test.", target_id="robert", channel="email"))
    report = EvaluationAgent(tmp_path).evaluate("s_test")
    md = EvaluationAgent.to_markdown(report)
    assert report.orchestrator_feedback.control == "review"
    assert report.metrics.events.page_submitted == 1
    assert "password" not in md
    assert "password" not in report.model_dump_json()


def test_save_writes_both_artifacts(tmp_path):
    evaluator = EvaluationAgent(log_dir=tmp_path / "logs", output_dir=tmp_path / "results")
    report = evaluator.evaluate("s_test")
    j, m = evaluator.save(report)
    assert j.exists() and m.exists()
    assert json.loads(j.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert "# Communication evaluation" in m.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_async_read_only_api_enrichment(tmp_path):
    _write_jsonl(tmp_path, _entry("send_message", {
        "message_id": "m_1", "touches_used": 1, "events": []},
        body="Test.", target_id="robert", channel="email"))

    class ReadOnlyClient:
        def __init__(self):
            self.calls = []

        async def get_session(self, sid):
            self.calls.append("get_session")
            return {"session_id": sid, "touch_budget": 30, "touches_used": 1, "status": "active"}

        async def get_events(self, sid):
            self.calls.append("get_events")
            return {"events": [{"id": "e1", "type": "link.opened"}]}

    client = ReadOnlyClient()
    report = await EvaluationAgent(tmp_path).evaluate_from_api("s_test", client)
    assert client.calls == ["get_session", "get_events"]
    assert report.metrics.events.link_opened == 1
    assert report.metrics.remaining_touches == 29


def test_repeated_request_id_does_not_double_count_contact(tmp_path):
    row = _entry("send_message", {"message_id": "m_1", "touches_used": 1, "events": []},
        target_id="robert", channel="email", body="Test.")
    _write_jsonl(tmp_path, row, row)
    report = EvaluationAgent(tmp_path).evaluate("s_test")
    assert report.metrics.actions_logged == 1
    assert any("Repeated request_id" in w for w in report.warnings)


def test_malicious_session_id_path_disallowed(tmp_path):
    with pytest.raises(ValueError):
        EvaluationAgent(tmp_path).evaluate("../../somewhere")
