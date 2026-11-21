"""In-memory simulator for tests and optional offline dashboard verification."""
import json

import httpx

from backend.main import API, create_app
from backend.conversations import LIVE_CHANNELS, MAX_TURNS

MOCK_KEY = "fixture-credential"


class MockSimulator:
    def __init__(self):
        self.sessions = {"s_eval": {
            "session_id": "s_eval", "scenario_id": "eval", "touch_budget": 10,
            "touches_used": 0, "status": "active",
        }}
        self.events = {}
        self.conversations = {}
        self.receipts = {}
        self.requests = []
        self.fail_once = None
        self.fail_create = False

    def accepted(self, kind, key, result):
        self.receipts[key] = result
        if self.fail_once == kind:
            self.fail_once = None
            raise httpx.ReadTimeout("Mock timeout after acceptance")
        return httpx.Response(200, json=result)

    def start_conversation(self, sid, request):
        key = request.headers["Idempotency-Key"]
        if key in self.receipts:
            return httpx.Response(200, json=self.receipts[key])
        payload = json.loads(request.content)
        assert payload["to"] in {"robin", "sam"}
        assert payload["channel"] in LIVE_CHANNELS
        session = self.sessions[sid]
        if session["touches_used"] >= session["touch_budget"]:
            return httpx.Response(409)
        for call in self.conversations.values():
            if call["session"] == sid and call["target"] == payload["to"]:
                call["ended"] = True
        cid = f"c_{len(self.conversations) + 1}"
        self.conversations[cid] = {"session": sid, "target": payload["to"], "turns": 0, "ended": False}
        session["touches_used"] += 1
        return self.accepted("conversation_start", key, {
            "conversation_id": cid, "status": "answered", "persona_says": "Hello, how can I help?",
            "touches_used": session["touches_used"],
        })

    def conversation_request(self, cid, action, request):
        assert request.method == "POST"
        call = self.conversations.get(cid)
        if call is None:
            return httpx.Response(404)
        if action == "end":
            assert not request.content
            if call["ended"]:
                return httpx.Response(409)
            call["ended"] = True
            if self.fail_once == "conversation_end":
                self.fail_once = None
                raise httpx.ReadTimeout("Mock timeout after closing")
            return httpx.Response(204)

        assert action == "turns"
        key = request.headers["Idempotency-Key"]
        if key in self.receipts:
            return httpx.Response(200, json=self.receipts[key])
        if call["ended"] or call["turns"] >= MAX_TURNS:
            return httpx.Response(409)
        payload = json.loads(request.content)
        assert set(payload) == {"say"}
        call["turns"] += 1
        number = call["turns"]
        call["ended"] = payload["say"] == "Goodbye."
        event = {
            "id": f"e_{cid}_{number}", "type": "link.opened", "conversation_id": cid,
            "url": "https://example.invalid/fixture",
        }
        self.events[call["session"]].append(event)
        return self.accepted("conversation_turn", key, {
            "persona_says": "Goodbye." if call["ended"] else "Thanks for the practice turn.",
            "ended": call["ended"], "events": [event], "turns_used": number,
        })

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
            return httpx.Response(200, json={"message": ["email", "sms"], "conversation": list(LIVE_CHANNELS)})
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

        prefix, sid, *suffix = path.split("/")
        if prefix == "conversations":
            assert len(suffix) == 1
            return self.conversation_request(sid, suffix[0], request)
        if sid not in self.sessions:
            return httpx.Response(404)
        if not suffix:
            return httpx.Response(200, json=self.sessions[sid])
        if suffix == ["events"]:
            return httpx.Response(200, json={"events": self.events[sid]})
        if suffix == ["conversations"]:
            assert request.method == "POST"
            return self.start_conversation(sid, request)
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
        return self.accepted("message", key, result)


def create_mock_app():
    return create_app(MOCK_KEY, transport=httpx.MockTransport(MockSimulator()))
