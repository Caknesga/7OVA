"""In-memory simulator for tests and optional offline dashboard verification."""
import json

import httpx

from backend.main import API, create_app

MOCK_KEY = "fixture-credential"


class MockSimulator:
    def __init__(self):
        self.sessions = {"s_eval": {
            "session_id": "s_eval", "scenario_id": "eval", "touch_budget": 10,
            "touches_used": 0, "status": "active",
        }}
        self.events = {}
        self.receipts = {}
        self.requests = []
        self.fail_once = None
        self.fail_create = False

    def __call__(self, request):
        assert str(request.url).startswith(API + "/")
        assert request.headers["Authorization"] == f"Bearer {MOCK_KEY}"
        assert set(request.extensions["timeout"].values()) == {120}
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1/")
        if path == "scenarios":
            return httpx.Response(200, json=[
                {"id": "practice", "open": True, "company": "Example Studio", "targets": [
                    {"id": "robin", "name": "Robin Park", "role": "Practice persona"},
                    {"id": "sam", "name": "Sam Lee", "role": "Practice persona"},
                ]},
                {"id": "eval", "open": True},
            ])
        if path == "channels":
            return httpx.Response(200, json={"message": ["email", "sms"], "conversation": ["phone_call"]})
        if path == "sessions":
            if request.method == "GET":
                return httpx.Response(200, json=list(reversed(self.sessions.values())))
            assert request.headers["Idempotency-Key"]
            assert json.loads(request.content) == {"scenario_id": "practice"}
            sid = f"s_{len(self.sessions)}"
            self.sessions[sid] = {
                "session_id": sid, "scenario_id": "practice", "touch_budget": 30,
                "touches_used": 0, "status": "active",
            }
            self.events[sid] = []
            if self.fail_create:
                raise httpx.ReadTimeout("Mock timeout after creation")
            return httpx.Response(201, json=self.sessions[sid])

        _, sid, *suffix = path.split("/")
        if sid not in self.sessions:
            return httpx.Response(404)
        if not suffix:
            return httpx.Response(200, json=self.sessions[sid])
        if suffix == ["events"]:
            return httpx.Response(200, json={"events": self.events[sid]})
        assert suffix == ["messages"] and request.method == "POST"
        key = request.headers["Idempotency-Key"]
        if key in self.receipts:
            return httpx.Response(200, json=self.receipts[key])
        payload = json.loads(request.content)
        assert payload["to"] in {"robin", "sam"}
        assert payload["channel"] in {"email", "sms"}
        self.sessions[sid]["touches_used"] += 1
        count = self.sessions[sid]["touches_used"]
        result = {"message_id": f"m_{count}", "touches_used": count, "events": [
            {"id": f"e_{count}_reply", "type": "reply", "channel": payload["channel"],
             "in_reply_to": f"m_{count}", "body": "Thanks for the practice message."},
            {"id": f"e_{count}_link", "type": "link.opened",
             "message_id": f"m_{count}", "url": "https://example.invalid/fixture"},
        ]}
        self.events[sid].extend(result["events"])
        self.receipts[key] = result
        if self.fail_once == "message":
            self.fail_once = None
            raise httpx.ReadTimeout("Mock timeout after acceptance")
        return httpx.Response(200, json=result)


def create_mock_app():
    return create_app(MOCK_KEY, transport=httpx.MockTransport(MockSimulator()))
