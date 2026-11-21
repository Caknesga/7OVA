from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from tests.mock_simulator import MOCK_KEY, MockSimulator

PAYLOAD = {"to": "robin", "channel": "email", "body": "Hello from the practice console."}


def operation_key():
    return {"Idempotency-Key": str(uuid4())}


@pytest.fixture
def simulator(tmp_path):
    mock = MockSimulator()
    app = create_app(MOCK_KEY, tmp_path / "history.sqlite3", httpx.MockTransport(mock))
    return mock, app


def test_practice_flow_is_saved_and_replies_are_linked(simulator):
    mock, app = simulator
    with TestClient(app) as client:
        assert client.get("/api/status").json() == {"configured": True, "scenario": "practice"}
        assert [scenario["id"] for scenario in client.get("/api/scenarios").json()] == ["practice"]
        assert client.get("/api/channels").json() == ["email", "sms"]
        create_key = operation_key()
        session = client.post("/api/sessions", headers=create_key)
        assert session.status_code == 201
        assert client.post("/api/sessions", headers=create_key).json() == session.json()
        sid = session.json()["session_id"]
        path = f"/api/sessions/{sid}"
        assert [item["session_id"] for item in client.get("/api/sessions").json()] == [sid]
        key = operation_key()
        response = client.post(f"{path}/messages", headers=key, json=PAYLOAD)
        assert response.status_code == 200
        assert client.post(f"{path}/messages", headers=key, json=PAYLOAD).json() == response.json()
        assert client.post(f"{path}/messages", headers=key, json={**PAYLOAD, "body": "Edited"}).status_code == 409
        assert client.get(path).json()["touches_used"] == 1
        assert len(client.get(f"{path}/events").json()) == 2
        assert len(client.get(f"{path}/events").json()) == 2

    with TestClient(app) as restarted:
        history = restarted.get(f"{path}/history").json()
        assert history["messages"] == [{**PAYLOAD, "message_id": "m_1"}]
        assert len(history["events"]) == 2
        assert history["events"][0]["in_reply_to"] == history["messages"][0]["message_id"]
        assert sum(request.url.path.endswith("/messages") for request in mock.requests) == 1


def test_timeout_retries_same_message_without_spending_another_touch(simulator):
    mock, app = simulator
    with TestClient(app) as client:
        sid = client.post("/api/sessions", headers=operation_key()).json()["session_id"]
        path = f"/api/sessions/{sid}"
        mock.fail_once = "message"
        key = operation_key()
        assert client.post(f"{path}/messages", headers=key, json=PAYLOAD).status_code == 504
        assert client.get(f"{path}/history").json()["messages"] == []
        assert client.post(f"{path}/messages", headers=key, json=PAYLOAD).status_code == 200
        assert client.get(path).json()["touches_used"] == 1


def test_uncertain_creation_does_not_repeat_upstream(simulator):
    mock, app = simulator
    with TestClient(app) as client:
        mock.fail_create = True
        key = operation_key()
        assert client.post("/api/sessions", headers=key).status_code == 504
        assert client.post("/api/sessions", headers=key).status_code == 409
        assert len(client.get("/api/sessions").json()) == 1


def test_eval_and_unrecognized_payload_fields_cannot_send(simulator):
    mock, app = simulator
    with TestClient(app) as client:
        assert client.get("/api/sessions/s_eval").status_code == 404
        assert client.get("/api/sessions/s_eval/events").status_code == 404
        assert client.post("/api/sessions/s_eval/messages", headers=operation_key(), json=PAYLOAD).status_code == 404
        assert client.post("/api/sessions/s_eval/messages", headers=operation_key(),
                           json={**PAYLOAD, "url": "https://example.invalid"}).status_code == 422
        assert not any(request.method == "POST" for request in mock.requests)


def test_upstream_redirect_is_rejected_and_credentials_are_not_relayed(tmp_path):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://example.invalid/"}, text=MOCK_KEY)

    app = create_app(MOCK_KEY, tmp_path / "history.sqlite3", httpx.MockTransport(redirect))
    with TestClient(app) as client:
        response = client.get("/api/scenarios")
        assert response.status_code == 502
        assert MOCK_KEY not in response.text
        assert len(requests) == 1


def test_missing_key_does_not_contact_simulator(tmp_path):
    def forbidden(request):
        pytest.fail("Unexpected simulator request")

    app = create_app("", tmp_path / "history.sqlite3", httpx.MockTransport(forbidden))
    with TestClient(app) as client:
        assert client.get("/api/status").json()["configured"] is False
        assert client.get("/api/scenarios").status_code == 503
        assert client.post("/api/sessions", headers=operation_key()).status_code == 503
