from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.qa_simulator import MOCK_TARGET, MockSimulator


def test_reply_and_silence_are_local_fixtures():
    for mode in ("reply", "silence"):
        simulator = MockSimulator(reply_mode=mode)
        session_id = simulator.session()["session_id"]
        response = simulator.execute(
            "send_message", session_id, MOCK_TARGET, "email", "This is a test.", None, "first",
        )
        assert response["touches_used"] == 1
        if mode == "silence":
            assert response["events"] == []
        else:
            reply, = response["events"]
            assert reply["type"] == "reply"
            assert reply["in_reply_to"] == response["message_id"]


def test_timeout_retry_recovers_original_receipt_after_touch_limit():
    simulator = MockSimulator(touch_budget=1, timeout_after_acceptance="send_message")
    session_id = simulator.session()["session_id"]
    request = ("send_message", session_id, MOCK_TARGET, "email", "This is a test.", None, "first")
    with pytest.raises(HTTPException) as timeout:
        simulator.execute(*request)
    assert timeout.value.status_code == 504
    receipt = simulator.execute(*request)
    assert simulator.execute(*request) == receipt
    assert simulator.session(session_id)["touches_used"] == 1

    with pytest.raises(HTTPException) as mismatched:
        simulator.execute(*request[:4], "A different test.", None, "first")
    assert mismatched.value.status_code == 409
    assert "key" in mismatched.value.detail

    with pytest.raises(HTTPException) as exhausted:
        simulator.execute(*request[:-1], "second")
    assert exhausted.value.status_code == 409
    assert "touches" in exhausted.value.detail


def test_live_conversation_takes_one_touch_and_cannot_escape_session_scope():
    simulator = MockSimulator(end_after_turns=10, touch_budget=1)
    session_id = simulator.session()["session_id"]
    greeting = simulator.execute("start_conversation", session_id, MOCK_TARGET, "live_chat", None, None, "start")
    conversation_id = greeting["conversation_id"]
    assert greeting["persona_says"] and greeting["turns_used"] == 0

    other_session = simulator.session()["session_id"]
    with pytest.raises(HTTPException) as scope:
        simulator.execute(
            "continue_conversation", other_session, MOCK_TARGET, "live_chat", "Test", conversation_id, "scope",
        )
    assert scope.value.status_code == 404

    args = ("continue_conversation", session_id, MOCK_TARGET, "live_chat", "Test", conversation_id)
    for turn in range(1, 11):
        response = simulator.execute(*args, uuid4().hex)
        assert response["turns_used"] == turn
        assert response["touches_used"] == 1
        assert response["ended"] == (turn == 10)
    with pytest.raises(HTTPException) as ended:
        simulator.execute(*args, "extra")
    assert ended.value.status_code == 409
    receipt = simulator.execute("end_conversation", session_id, MOCK_TARGET, "live_chat", None, conversation_id, "end")
    assert receipt["ended"]
    assert receipt["touches_used"] == 1
