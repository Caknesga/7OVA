import time
from threading import Event
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.qa import PHRASES
from backend.qa_openai import Settings, provider
from backend.qa_simulator import MockSimulator
from tests.mock_simulator import MOCK_KEY, MockSimulator as TargetTransport


def key():
    return {"Idempotency-Key": str(uuid4())}


def start(client, *, mode="mock", target="qa_persona", sid=None, limit=2, confirm=False, headers=None):
    return client.post("/api/qa/runs", headers=headers or key(), json={
        "mode": mode, "target": target, "session_id": sid, "max_touches": limit,
        "confirm_practice": confirm,
    })


def completed(client, run_id):
    until = time.monotonic() + 4
    while time.monotonic() < until:
        run = client.get("/api/qa/runs/" + run_id).json()
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("QA run did not stop")


def offline(request):
    raise AssertionError("Unexpected outbound request")


def test_mock_workflow_retry_live_turn_silence_and_persistence(tmp_path):
    db = tmp_path / "history.sqlite3"
    options = {"simulator_factory": lambda: MockSimulator(
        reply_mode="silence", timeout_after_acceptance="start_conversation"
    ), "retry_delay": 0}
    app = create_app("", db, httpx.MockTransport(offline), qa_options=options)
    request_key = key()
    with TestClient(app) as client:
        r = start(client, headers=request_key)
        assert r.status_code == 202
        run_id = r.json()["id"]
        assert start(client, headers=request_key).json()["id"] == run_id
        assert start(client, headers=request_key, limit=1).status_code == 409
        run = completed(client, run_id)
        assert run["status"] == "completed", run
        assert run["touches_used"] == 2
        assert run["usage"]["input_tokens"] > 0
        assert run["usage"]["estimated_usd"] == 0
        states = {item["state"] for item in run["timeline"]}
        assert states == {"START", "RESEARCH", "PLAN", "GENERATE", "COMMUNICATE",
                          "EVALUATE", "MEMORY", "CONTINUE", "STOP"}
        details = [item["detail"] for item in run["timeline"]]
        assert any(d.get("retry") for d in details)
        assert any(d.get("observation") == "silence" for d in details)
        assert any(d.get("observation") == "reply" for d in details)
        assert any(d.get("turns_used") == 1 for d in details)
        assert len(client.get("/api/qa/runs").json()) == 1
    with TestClient(create_app("", db, httpx.MockTransport(offline))) as client:
        assert client.get("/api/qa/runs/" + run_id).json() == run


def practice_settings(**changes):
    return Settings(**({"api_key": "unused", "model": "fixture",
                        "input_rate": 1, "output_rate": 1} | changes))


def test_real_adapter_confirmation_practice_guard_and_official_routes(tmp_path):
    upstream = TargetTransport()
    with TestClient(create_app(MOCK_KEY, tmp_path / "db", httpx.MockTransport(upstream),
                               qa_options={"settings": practice_settings(),
                                           "provider_factory": lambda s, _: provider(s, True)})) as client:
        assert start(client, mode="practice", target="robin", sid="s_eval").status_code == 422
        rejected = start(client, mode="practice", target="robin", sid="s_eval", confirm=True)
        assert completed(client, rejected.json()["id"])["status"] == "failed"
        assert not [req for req in upstream.requests if req.method == "POST"]
        sid = client.post("/api/sessions", headers=key()).json()["session_id"]
        r = start(client, mode="practice", target="robin", sid=sid, confirm=True)
        run = completed(client, r.json()["id"])
        assert run["status"] == "completed", run
        contacts = [req for req in upstream.requests if req.method == "POST"]
        assert [req.url.path.rsplit("/", 1)[-1] for req in contacts] == [
            "sessions", "conversations", "turns", "end", "messages",
        ]
        bodies = [req.content.decode() for req in contacts]
        assert any(PHRASES["hello"] in body for body in bodies)
        assert any(PHRASES["availability"] in body for body in bodies)
        assert client.get(f"/api/sessions/{sid}").json()["touches_used"] == 2
        assert run["usage"]["estimated_usd"] > 0


def test_budget_reservation_prevents_openai_call(tmp_path):
    upstream = TargetTransport()
    with TestClient(create_app(MOCK_KEY, tmp_path / "db", httpx.MockTransport(upstream),
                               qa_options={"settings": practice_settings(max_run_cost_usd=0.000001),
                                           "provider_factory": lambda *_: offline})) as client:
        sid = client.post("/api/sessions", headers=key()).json()["session_id"]
        r = start(client, mode="practice", target="robin", sid=sid, confirm=True)
        run = completed(client, r.json()["id"])
        assert run["status"] == "stopped"
        assert "per-run budget" in run["stop_reason"]
        assert len([req for req in upstream.requests if req.method == "POST"]) == 1


def test_stop_prevents_new_contact_after_pending_inference(tmp_path):
    entered, release = Event(), Event()

    def llm(s, mock):
        choose = provider(s, mock)

        def blocked(*args):
            entered.set()
            assert release.wait(3)
            return choose(*args)
        return blocked

    with TestClient(create_app("", tmp_path / "db", httpx.MockTransport(offline),
                               qa_options={"provider_factory": llm})) as client:
        run_id = start(client).json()["id"]
        assert entered.wait(2)
        assert client.post(f"/api/qa/runs/{run_id}/stop", headers=key()).status_code == 200
        release.set()
        run = completed(client, run_id)
        assert run["status"] == "stopped"
        assert run["touches_used"] == 0
        assert not any(item["state"] == "COMMUNICATE" for item in run["timeline"])
