"""No live calls to OpenAI or the Target API are made by these tests."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from planning.agent import PlannerInput, PlanningAgent


EXAMPLE_FILE = Path(__file__).with_name("example_input.json")


class FakeResponses:
    def __init__(self, choice):
        self.choice = choice
        self.calls = 0
        self.payload = None

    async def parse(self, **kwargs):
        self.calls += 1
        self.payload = kwargs
        model = kwargs["text_format"]
        return SimpleNamespace(output_parsed=model(
            action=self.choice,
            reasoning_summary="Validate benign simulator API behavior.",
            expected_observable_events=["reply"],
            confidence=0.92,
        ))


def example():
    return json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_email_action_matches_communication_schema():
    responses = FakeResponses("email_receipt")
    result = await PlanningAgent(client=SimpleNamespace(responses=responses)).plan(example())
    assert result.decision == "execute"
    assert result.planned_action.action_type == "send_message"
    assert result.planned_action.channel == "email"
    assert result.planned_action.target_id == "test_persona"
    assert "password" not in result.planned_action.body.lower()
    assert responses.calls == 1
    prompt = json.loads(responses.payload["input"][1]["content"])
    assert "full_report_markdown" not in prompt


@pytest.mark.asyncio
async def test_active_conversation_turn():
    data = example()
    data["simulator_environment"].update(
        conversation_active=True, conversation_id="c123", remaining_conversation_turns=7
    )
    result = await PlanningAgent(client=SimpleNamespace(responses=FakeResponses("send_live_turn"))).plan(data)
    assert result.planned_action.action_type == "send_turn"
    assert result.planned_action.say


@pytest.mark.asyncio
async def test_end_conversation():
    data = example()
    data["simulator_environment"].update(
        conversation_active=True, conversation_id="c123", remaining_conversation_turns=0
    )
    result = await PlanningAgent(client=SimpleNamespace(responses=FakeResponses("end_live_chat"))).plan(data)
    assert result.planned_action.action_type == "end_conversation"


@pytest.mark.asyncio
async def test_stop_on_completed_objective_no_model_call():
    data = example()
    data["qa_objective"]["completion_met"] = True
    result = await PlanningAgent(client=None).plan(data)
    assert result.decision == "stop"
    assert result.planned_action is None


@pytest.mark.asyncio
async def test_stop_on_exhausted_budget_no_model_call():
    data = example()
    data["simulator_environment"]["remaining_touch_budget"] = 0
    result = await PlanningAgent(client=None).plan(data)
    assert result.decision == "stop"


@pytest.mark.asyncio
async def test_stop_on_last_evaluation_review():
    data = example()
    data["interaction_history"]["last_evaluation"] = {"orchestrator_feedback": {"control": "review"}}
    result = await PlanningAgent(client=None).plan(data)
    assert result.decision == "stop"


@pytest.mark.asyncio
async def test_reject_unavailable_channel():
    data = example()
    data["simulator_environment"]["allowed_channels"] = ["live_chat"]
    with pytest.raises(ValueError, match="unavailable"):
        await PlanningAgent(client=SimpleNamespace(responses=FakeResponses("email_receipt"))).plan(data)


def test_reject_target_identity_mismatch():
    data = example()
    data["research_report"]["target"]["id"] = "someone_else"
    with pytest.raises(ValueError, match="differ"):
        PlannerInput.model_validate(data)


@pytest.mark.asyncio
async def test_max_actions_stops_without_model():
    data = example()
    data["operational_constraints"]["actions_used"] = 10
    result = await PlanningAgent(client=None).plan(data)
    assert result.decision == "stop"


@pytest.mark.asyncio
async def test_fresh_session_refusal_bootstraps_transparent_qa():
    data = example()
    data["research_report"]["research_metadata"]["status"] = "not_run"
    fake = FakeResponses("insufficient_information")
    result = await PlanningAgent(client=SimpleNamespace(responses=fake)).plan(data)
    assert fake.calls == 1
    assert result.decision == "execute"
    assert result.planned_action.action_type == "start_conversation"
    assert result.planned_action.channel == "live_chat"


@pytest.mark.asyncio
async def test_refusal_with_missing_test_channel_still_refuses():
    data = example()
    data["simulator_environment"]["allowed_channels"] = []
    result = await PlanningAgent(client=None).plan(data)
    assert result.decision == "stop"


@pytest.mark.asyncio
async def test_existing_history_does_not_trigger_bootstrap():
    data = example()
    data["interaction_history"]["previous_actions"] = [{"action_type": "send_message"}]
    result = await PlanningAgent(client=SimpleNamespace(responses=FakeResponses("insufficient_information"))).plan(data)
    assert result.decision == "insufficient_information"
