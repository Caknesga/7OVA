"""Run with: python -m pytest communication/test_agent.py -q

All tests use httpx.MockTransport and consume no live simulator touches.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from .agent import CommunicationAgent
from .client import TargetAPIClient


SCENARIOS = [
    {
        "id": "practice",
        "kind": "practice",
        "company": "revel8",
        "open": True,
        "targets": [{"id": "robert", "name": "Robert Seilbeck", "role": "CTO"}],
        "touch_budget": 30,
    }
]
CHANNELS = [
    {"id": "email", "kind": "message"},
    {"id": "phone_call", "kind": "live"},
]
SESSION = {
    "session_id": "s_test",
    "scenario_id": "practice",
    "touch_budget": 30,
    "touches_used": 0,
    "status": "active",
}


def make_transport(*, invalid_message_response=False, sent=None):
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/channels":
            return httpx.Response(200, json=CHANNELS)
        if method == "GET" and path == "/v1/scenarios":
            return httpx.Response(200, json=SCENARIOS)
        if method == "GET" and path == "/v1/sessions/s_test":
            return httpx.Response(200, json=SESSION)
        if method == "POST":
            body = json.loads(request.content) if request.content else {}
            sent.append((path, body, request.headers.get("Idempotency-Key")))
            if path == "/v1/sessions/s_test/messages":
                if invalid_message_response:
                    return httpx.Response(200, json={"events": []})
                return httpx.Response(200, json={
                    "message_id": "m_test", "touches_used": 1,
                    "events": [{"id": "e_1", "type": "reply", "body": "Received."}],
                })
            if path == "/v1/sessions/s_test/conversations":
                return httpx.Response(201, json={
                    "conversation_id": "c_test", "status": "answered",
                    "persona_says": "Hello?", "touches_used": 1,
                })
            if path == "/v1/conversations/c_test/turns":
                return httpx.Response(200, json={
                    "persona_says": "Understood.", "ended": False,
                    "turns_used": 1, "events": [],
                })
            if path == "/v1/conversations/c_test/end":
                return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_send_message_keeps_content_and_request_id():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(
            {"action_type": "send_message", "target_id": "robert", "channel": "email",
             "sender": {"name": "Training Team", "address": "training@example.org"},
             "subject": "Test", "body": "Please confirm receipt."},
            {"session_id": "s_test", "request_id": "req-1", "remaining_touches": 30},
        )
    assert result.success is True
    assert result.message_id == "m_test"
    assert result.events[0]["body"] == "Received."
    assert sent[0][1]["body"] == "Please confirm receipt."
    assert sent[0][1]["from"]["name"] == "Training Team"
    assert sent[0][2] == "req-1"
    assert result.touches_used == 1


@pytest.mark.asyncio
async def test_start_conversation_and_send_turn_and_end():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        started = await agent.execute(
            {"action_type": "start_conversation", "target_id": "robert", "channel": "phone_call"},
            {"session_id": "s_test", "request_id": "req-start"},
        )
        assert started.success is True
        assert started.conversation_id == "c_test"
        assert started.persona_says == "Hello?"

        turn = await agent.execute(
            {"action_type": "send_turn", "say": "This is an authorized simulation test."},
            {"session_id": "s_test", "conversation_id": "c_test", "request_id": "req-turn",
             "remaining_conversation_turns": 10},
        )
        assert turn.success is True
        assert turn.turns_used == 1
        assert turn.ended is False
        assert sent[1][2] == "req-turn"

        end = await agent.execute(
            {"action_type": "end_conversation"},
            {"session_id": "s_test", "conversation_id": "c_test"},
        )
        assert end.success is True
        assert end.ended is True


@pytest.mark.asyncio
async def test_unknown_target_rejected_without_contact():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(
            {"action_type": "send_message", "target_id": "unknown", "channel": "email",
             "body": "Hello"},
            {"session_id": "s_test", "request_id": "req-2"},
        )
    assert not result.success
    assert "UNKNOWN_TARGET" in result.error
    assert sent == []


@pytest.mark.asyncio
async def test_wrong_channel_kind_rejected_without_contact():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        with pytest.raises(ValidationError, match="cannot use a live conversation channel"):
            await agent.execute(
                {"action_type": "send_message", "target_id": "robert", "channel": "phone_call",
                 "body": "Hello"},
                {"session_id": "s_test", "request_id": "req-3"},
            )
    assert sent == []


@pytest.mark.asyncio
async def test_budget_exhausted_without_contact():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(
            {"action_type": "send_message", "target_id": "robert", "channel": "email",
             "body": "Hello"},
            {"session_id": "s_test", "request_id": "req-4", "remaining_touches": 0},
        )
    assert not result.success
    assert "TOUCH_BUDGET_EXHAUSTED" in result.error
    assert sent == []


@pytest.mark.asyncio
async def test_missing_request_id_rejected_without_contact():
    sent = []
    async with TargetAPIClient(team_key="test-key", transport=make_transport(sent=sent)) as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(
            {"action_type": "send_message", "target_id": "robert", "channel": "email",
             "body": "Hello"},
            {"session_id": "s_test"},
        )
    assert not result.success
    assert "MISSING_REQUEST_ID" in result.error
    assert sent == []


@pytest.mark.asyncio
async def test_malformed_response_is_failure_not_success():
    async with TargetAPIClient(
        team_key="test-key", transport=make_transport(invalid_message_response=True)
    ) as client:
        agent = CommunicationAgent(client)
        result = await agent.execute(
            {"action_type": "send_message", "target_id": "robert", "channel": "email",
             "body": "Hello"},
            {"session_id": "s_test", "request_id": "req-5"},
        )
    assert not result.success
    assert "SimulatorResponseError" in result.error


def test_malformed_planner_input_raises_validation_error():
    with pytest.raises(ValidationError):
        asyncio.run(_invalid_input())


async def _invalid_input():
    async with TargetAPIClient(team_key="test-key", transport=make_transport()) as client:
        return await CommunicationAgent(client).execute(
            {"action_type": "send_message", "target_id": "robert"},
            {"session_id": "s_test", "request_id": "req-6"},
        )
