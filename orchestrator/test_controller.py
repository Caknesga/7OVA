import json
from pathlib import Path

import pytest

from orchestrator.controller import Orchestrator, ScriptedTestPlanner
from orchestrator.state import Phase, RunState


def sample_action(channel="email"):
    return {
        "action_type": "send_message", "target_id": "test_persona", "channel": channel,
        "sender": {"name": "QA Tester", "address": "qa@example.org"},
        "subject": "Interface test", "body": "Please acknowledge this test message.",
    }


def make_controller(tmp_path: Path, actions, **kwargs):
    return Orchestrator(
        ScriptedTestPlanner(actions),
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
        evaluation_dir=tmp_path / "reports",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_single_action_logged_and_evaluated(tmp_path):
    state = RunState(session_id="qa_first", touch_budget=3)
    out = await make_controller(tmp_path, [sample_action()]).run(state)
    assert out.phase == Phase.COMPLETED
    assert out.touches_used == 1
    assert out.steps_completed == 1
    assert out.last_evaluation["control"] == "decision_required"
    records = [json.loads(s) for s in (tmp_path / "logs" / "qa_first.jsonl").read_text().splitlines()]
    assert records[0]["planned_action"]["body"] == sample_action()["body"]
    assert records[0]["communication_result"]["events"][0]["type"] == "reply"
    assert (tmp_path / "reports" / "qa_first_evaluation.md").exists()
    assert (tmp_path / "state" / "qa_first.json").exists()


@pytest.mark.asyncio
async def test_budget_blocks_second_message(tmp_path):
    controller = make_controller(tmp_path, [sample_action(), sample_action()])
    out = await controller.run(RunState(session_id="qa_budget", touch_budget=1))
    assert out.phase == Phase.STOPPED
    assert out.steps_completed == 1
    assert out.touches_used == 1
    assert "remaining session contacts" in out.last_error.lower()


@pytest.mark.asyncio
async def test_empty_planner_completes_without_contact(tmp_path):
    out = await make_controller(tmp_path, []).run(RunState(session_id="qa_empty"))
    assert out.phase == Phase.COMPLETED
    assert out.steps_completed == 0
    assert out.touches_used == 0


@pytest.mark.asyncio
async def test_live_chat_start_turn_end(tmp_path):
    actions = [
        {"action_type": "start_conversation", "target_id": "test_persona", "channel": "live_chat"},
        {"action_type": "send_turn", "say": "Hello, this is a chat test."},
        {"action_type": "end_conversation"},
    ]
    out = await make_controller(tmp_path, actions).run(RunState(session_id="qa_chat", touch_budget=1))
    assert out.phase == Phase.STOPPED
    assert out.touches_used == 1
    assert out.steps_completed == 3
    assert not out.conversation_active
    assert out.turns_used == 1


@pytest.mark.asyncio
async def test_max_steps_stops(tmp_path):
    out = await make_controller(tmp_path, [sample_action(), sample_action()], max_steps=1).run(
        RunState(session_id="qa_max", touch_budget=5)
    )
    assert out.phase == Phase.STOPPED
    assert out.steps_completed == 1


def test_malicious_session_id_rejected():
    with pytest.raises(ValueError):
        RunState(session_id="../outside")


def test_no_live_transport_can_be_injected():
    with pytest.raises(TypeError):
        Orchestrator(ScriptedTestPlanner([]), communication=object())
