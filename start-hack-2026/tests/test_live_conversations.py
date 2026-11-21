import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.conversations import LIVE_CHANNELS, MAX_TURNS
from backend.main import create_app
from tests.mock_simulator import MOCK_KEY, MockSimulator


def post(client, path, payload=None, key=None):
    return client.post(path, headers={"Idempotency-Key": key or str(uuid4())}, json=payload)


@pytest.fixture
def practice(tmp_path):
    mock = MockSimulator()
    with TestClient(create_app(MOCK_KEY, tmp_path / "history.sqlite3", httpx.MockTransport(mock))) as client:
        sid = post(client, "/api/sessions").json()["session_id"]
        yield client, mock, f"/api/sessions/{sid}"


def start(client, session, channel="phone_call", *, key=None):
    return post(client, f"{session}/conversations", {"to": "robin", "channel": channel}, key)


def send_turn(client, cid, say, *, key=None):
    return post(client, f"/api/conversations/{cid}/turns", {"say": say}, key)


@pytest.mark.parametrize("channel", LIVE_CHANNELS)
def test_can_start_each_live_channel(practice, channel):
    client, _, session = practice
    response = start(client, session, channel)
    assert response.status_code == 201
    receipt = response.json()
    assert receipt["status"] == "answered"
    assert receipt["persona_says"] == "Hello, how can I help?"
    assert receipt["touches_used"] == 1
    assert receipt["turns_used"] == 0
    saved, = client.get(f"{session}/conversations").json()
    assert saved["conversation_id"] == receipt["conversation_id"]
    assert saved["channel"] == channel
    assert saved["greeting"] == receipt["persona_says"]
    assert saved["turns"] == []


def test_turns_events_and_manual_end(practice):
    client, mock, session = practice
    cid = start(client, session).json()["conversation_id"]
    for number in (1, 2):
        response = send_turn(client, cid, f"Practice turn {number}.")
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["conversation_id"] == cid
        assert receipt["turns_used"] == number
        assert receipt["persona_says"] == "Thanks for the practice turn."
        assert receipt["events"][0]["conversation_id"] == cid
    assert client.get(session).json()["touches_used"] == 1

    # Event refreshes reconcile the same IDs without dropping their conversation association.
    for event in mock.events[session.split("/")[-1]]:
        event.pop("conversation_id")
    for _ in range(2):
        assert client.get(f"{session}/events").status_code == 200
    saved, = client.get(f"{session}/conversations").json()
    assert [turn["number"] for turn in saved["turns"]] == [1, 2]
    assert [event["conversation_id"] for event in saved["events"]] == [cid, cid]
    assert len(client.get(f"{session}/history").json()["events"]) == 2

    for _ in range(2):
        ended = post(client, f"/api/conversations/{cid}/end")
        assert ended.status_code == 200
        assert ended.json()["ended"] is True
        assert ended.json()["turns_used"] == 2
    assert send_turn(client, cid, "One more turn.").status_code == 409
    saved, = client.get(f"{session}/conversations").json()
    assert saved["ended"] is True
    assert saved["status"] == "ended"
    assert sum(req.url.path.endswith("/end") for req in mock.requests) == 1


def test_tenth_turn_blocks_new_turns_but_allows_receipt_replay_and_end(practice):
    client, mock, session = practice
    cid = start(client, session).json()["conversation_id"]
    for number in range(1, MAX_TURNS + 1):
        key = str(uuid4())
        response = send_turn(client, cid, f"Turn {number}", key=key)
        assert response.status_code == 200
        assert response.json()["turns_used"] == number

    assert send_turn(client, cid, f"Turn {MAX_TURNS}", key=key).json() == response.json()
    blocked = send_turn(client, cid, "Past the limit")
    assert blocked.status_code == 409
    assert "10-turn limit" in blocked.json()["detail"]
    assert sum(req.url.path.endswith("/turns") for req in mock.requests) == MAX_TURNS
    assert post(client, f"/api/conversations/{cid}/end").json()["ended"] is True


def test_persona_can_end_a_conversation_and_new_start_replaces_same_target(practice):
    client, mock, session = practice
    first = start(client, session).json()["conversation_id"]
    second = start(client, session, "teams_call").json()["conversation_id"]
    snapshots = client.get(f"{session}/conversations").json()
    assert [(call["conversation_id"], call["ended"]) for call in snapshots] == [(first, True), (second, False)]
    assert send_turn(client, first, "Can you hear me?").status_code == 409
    goodbye = send_turn(client, second, "Goodbye.")
    assert goodbye.json()["persona_says"] == "Goodbye."
    assert goodbye.json()["ended"] is True
    assert send_turn(client, second, "One more thing").status_code == 409
    assert sum(req.url.path.endswith("/turns") for req in mock.requests) == 1


def test_timeout_retries_and_concurrent_duplicates_do_not_spend_extra_touches_or_turns(practice):
    client, mock, session = practice
    start_key = str(uuid4())
    mock.fail_once = "conversation_start"
    assert start(client, session, key=start_key).status_code == 504
    assert client.get(f"{session}/conversations").json() == []
    assert start(client, session, "video_call", key=start_key).status_code == 409
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, duplicate = list(workers.map(lambda _: start(client, session, key=start_key), range(2)))
    assert first.status_code == duplicate.status_code == 201
    assert first.json() == duplicate.json()
    cid = first.json()["conversation_id"]
    assert client.get(session).json()["touches_used"] == 1

    turn_key = str(uuid4())
    mock.fail_once = "conversation_turn"
    assert send_turn(client, cid, "A practice question.", key=turn_key).status_code == 504
    assert send_turn(client, cid, "An edited question.", key=turn_key).status_code == 409
    receipt = send_turn(client, cid, "A practice question.", key=turn_key).json()
    assert receipt["turns_used"] == 1

    mock.fail_once = "conversation_end"
    assert post(client, f"/api/conversations/{cid}/end").status_code == 504
    assert post(client, f"/api/conversations/{cid}/end").json()["ended"] is True
    assert send_turn(client, cid, "A practice question.", key=turn_key).json() == receipt
    assert sum(req.url.path.endswith("/conversations") for req in mock.requests) == 2
    assert sum(req.url.path.endswith("/turns") for req in mock.requests) == 2
    saved, = client.get(f"{session}/conversations").json()
    assert len(saved["events"]) == len(saved["turns"]) == 1


def test_live_requests_stay_in_practice_and_require_registered_conversations(practice):
    client, mock, session = practice
    assert start(client, "/api/sessions/s_eval").status_code == 404
    assert start(client, session, "email").status_code == 422
    assert send_turn(client, "c_unknown", "Hello").status_code == 404
    assert post(client, "/api/conversations/c_unknown/end").status_code == 404
    assert all(req.url.path == "/v1/sessions" or req.method == "GET" for req in mock.requests)

    cid = start(client, session).json()["conversation_id"]
    assert send_turn(client, cid, " ").status_code == 422
    assert send_turn(client, cid, "a" * 4001).status_code == 422
    assert send_turn(client, cid, MOCK_KEY).status_code == 422
    mock.conversations[cid]["ended"] = True
    assert send_turn(client, cid, "Still there?").status_code == 409
    saved, = client.get(f"{session}/conversations").json()
    assert saved["ended"] is True


def test_old_history_survives_and_live_transcript_is_available_after_restart(tmp_path):
    database = tmp_path / "history.sqlite3"
    old_message = {"message_id": "m_legacy", "body": "An existing message."}
    old_event = {"id": "e_legacy", "type": "reply", "in_reply_to": "m_legacy"}
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TABLE history (session TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                                  payload TEXT NOT NULL, UNIQUE(session, kind, id));
            CREATE TABLE operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT);
        """)
        db.executemany("INSERT INTO history VALUES ('s_1', ?, ?, ?)", [
            ("message", "m_legacy", json.dumps(old_message)),
            ("event", "e_legacy", json.dumps(old_event)),
        ])

    mock = MockSimulator()
    key = str(uuid4())
    session = "/api/sessions/s_1"
    with TestClient(create_app(MOCK_KEY, database, httpx.MockTransport(mock))) as client:
        assert post(client, "/api/sessions").json()["session_id"] == "s_1"
        cid = start(client, session, "live_chat").json()["conversation_id"]
        reply = send_turn(client, cid, "A saved turn.", key=key).json()
        assert post(client, f"/api/conversations/{cid}/end").status_code == 200

    def offline(_request):
        pytest.fail("A persisted transcript or receipt should not require the simulator")

    with TestClient(create_app(MOCK_KEY, database, httpx.MockTransport(offline))) as client:
        saved, = client.get(f"{session}/conversations").json()
        assert saved["ended"] is True
        assert saved["conversation_id"] == cid
        assert [turn["say"] for turn in saved["turns"]] == ["A saved turn."]
        assert saved["events"] == reply["events"]
        assert send_turn(client, cid, "A saved turn.", key=key).json() == reply
        history = client.get(f"{session}/history").json()
        assert history["messages"] == [old_message]
        assert history["events"] == [old_event, *reply["events"]]
