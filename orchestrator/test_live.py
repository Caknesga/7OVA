"""Network-free integration tests for the actual TargetAPIClient and communicator."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from communicate.client import TargetAPIClient
from orchestrator.controller import ScriptedTestPlanner
from orchestrator.live import QA_EMAIL, QA_SUBJECT, PracticeQAOrchestrator
from orchestrator.state import Phase
from planning.agent import PlannerResult
from communicate.schemas import PlannedAction


def qa_message():
    return {"action_type": "send_message", "target_id": "robert", "channel": "email",
            "sender": {"name": "QA Tester"}, "subject": QA_SUBJECT, "body": QA_EMAIL}


def inputs():
    return {
        "research_report": {"target": {"id": "robert", "name": "Robert Seilbeck"}},
        "qa_objective": {"description": "QA delivery test", "completion_criteria": ["An action is recorded"]},
        "simulator_environment": {"session_id": "placeholder", "target_id": "robert",
                                  "allowed_channels": ["email", "live_chat"], "remaining_touch_budget": 3},
        "interaction_history": {},
        "operational_constraints": {"permitted_sender": {"name": "QA Tester"},
                                    "max_actions_per_run": 5},
    }


def fake_transport():
    seen = {"used": 0, "requests": []}

    def handler(request):
        seen["requests"].append((request.method, request.url.path))
        path = request.url.path.removeprefix("/v1")
        if path == "/scenarios":
            return httpx.Response(200, json=[{"id": "practice", "open": True, "company": "revel8",
                                              "targets": [{"id": "robert", "name": "Robert Seilbeck"}]}])
        if path == "/channels":
            return httpx.Response(200, json=[{"id": "email", "kind": "message"},
                                              {"id": "live_chat", "kind": "live"}])
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(201, json={"session_id": "s_qa123", "scenario_id": "practice",
                                              "touch_budget": 3, "touches_used": 0, "status": "active"})
        if path == "/sessions/s_qa123" and request.method == "GET":
            return httpx.Response(200, json={"session_id": "s_qa123", "scenario_id": "practice",
                                             "touch_budget": 3, "touches_used": seen["used"], "status": "active"})
        if path == "/sessions/s_qa123/events":
            return httpx.Response(200, json={"events": []})
        if path == "/sessions/s_qa123/conversations" and request.method == "POST":
            seen["used"] += 1
            return httpx.Response(200, json={"conversation_id": "c_qa123", "status": "answered",
                                              "persona_says": "Hello, QA tester", "touches_used": seen["used"]})
        if path == "/conversations/c_qa123/turns" and request.method == "POST":
            return httpx.Response(200, json={"persona_says": "I received the test", "ended": False,
                                              "turns_used": 1, "events": []})
        if path == "/conversations/c_qa123/end" and request.method == "POST":
            return httpx.Response(200, json={})
        if path == "/sessions/s_qa123/messages" and request.method == "POST":
            assert request.headers.get("Idempotency-Key")
            payload = json.loads(request.content)
            assert payload["body"] == QA_EMAIL
            seen["used"] += 1
            return httpx.Response(200, json={"message_id": "m_123", "touches_used": seen["used"], "events": []})
        return httpx.Response(404, json={"detail": f"Unexpected {path}"})

    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_scripted_live_practice_uses_real_client_interface_without_network(tmp_path):
    transport, seen = fake_transport()
    async with TargetAPIClient(team_key="fake-test-key", transport=transport) as client:
        runner = PracticeQAOrchestrator(
            client=client, planner=ScriptedTestPlanner([qa_message()]),
            planner_inputs=inputs(),
            log_dir=tmp_path / "logs", state_dir=tmp_path / "state",
            evaluation_dir=tmp_path / "reports", max_steps=3,
        )
        state = await runner.prepare("robert")
        result = await runner.run(state)
    assert result.phase is Phase.COMPLETED
    assert result.touches_used == 1
    assert result.steps_completed == 1
    assert len([p for m, p in seen["requests"] if p.endswith("/messages")]) == 1
    log = json.loads((tmp_path / "logs/s_qa123.jsonl").read_text().splitlines()[0])
    assert log["communication_result"]["events"] == []
    assert log["communication_result"]["success"] is True
    assert log["planned_action"]["body"] == QA_EMAIL
    assert (tmp_path / "reports/s_qa123_evaluation.md").exists()
    assert result.pending_request_id is None


@pytest.mark.asyncio
async def test_unsafe_planner_message_is_rejected_before_send(tmp_path):
    transport, seen = fake_transport()
    bad = qa_message()
    bad["body"] = "Not a transparent QA message"
    async with TargetAPIClient(team_key="fake-test-key", transport=transport) as client:
        runner = PracticeQAOrchestrator(
            client=client, planner=ScriptedTestPlanner([bad]),
            planner_inputs=inputs(), log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state", evaluation_dir=tmp_path / "reports",
        )
        state = await runner.prepare("robert")
        result = await runner.run(state)
    assert result.phase is Phase.NEEDS_REVIEW
    assert result.steps_completed == 0
    assert not any(path.endswith("/messages") for _, path in seen["requests"])


@pytest.mark.asyncio
async def test_resume_with_pending_action_stops_without_duplicate_contact(tmp_path):
    transport, seen = fake_transport()
    async with TargetAPIClient(team_key="fake-test-key", transport=transport) as client:
        runner = PracticeQAOrchestrator(
            client=client, planner=ScriptedTestPlanner([qa_message()]),
            planner_inputs=inputs(), log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state", evaluation_dir=tmp_path / "reports",
        )
        state = await runner.prepare("robert")
        state.pending_request_id = "stable-request"
        state.pending_action = qa_message()
        runner._checkpoint(state)
        resumed = await runner.prepare("robert", existing_session_id=state.session_id)
        final = await runner.run(resumed)
    assert final.phase is Phase.NEEDS_REVIEW
    assert not any(path.endswith("/messages") for _, path in seen["requests"])


@pytest.mark.asyncio
async def test_max_steps_closes_live_practice_chat(tmp_path):
    transport, seen = fake_transport()
    actions = [
        {"action_type": "start_conversation", "target_id": "robert",
         "channel": "live_chat", "sender": {"name": "QA Tester"}},
        {"action_type": "send_turn", "say": "Hello, this is a simulated communication test. Is this message visible?"},
    ]
    async with TargetAPIClient(team_key="fake-test-key", transport=transport) as client:
        runner = PracticeQAOrchestrator(
            client=client, planner=ScriptedTestPlanner(actions),
            planner_inputs=inputs(), log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state", evaluation_dir=tmp_path / "reports",
            max_steps=2,
        )
        state = await runner.prepare("robert")
        result = await runner.run(state)
    assert result.phase is Phase.STOPPED
    assert result.steps_completed == 2
    assert result.touches_used == 1
    assert not result.conversation_active
    assert any(path.endswith("/conversations/c_qa123/end") for _, path in seen["requests"])
    records = [json.loads(row) for row in (tmp_path / "logs/s_qa123.jsonl").read_text().splitlines()]
    assert [r["planned_action"]["action_type"] for r in records] == [
        "start_conversation", "send_turn", "end_conversation"
    ]
