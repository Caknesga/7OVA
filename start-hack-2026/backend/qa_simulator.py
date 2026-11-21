"""Synthetic practice sessions for exercising the QA runner entirely offline."""

from copy import deepcopy
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException

from backend.conversations import MAX_TURNS

MOCK_TARGET = "qa_persona"


class MockSimulator:
    def __init__(
        self,
        *,
        reply_mode: Literal["reply", "silence"] = "reply",
        end_after_turns: int = 2,
        touch_budget: int = 30,
        timeout_after_acceptance: str | None = None,
    ):
        if reply_mode not in ("reply", "silence"):
            raise ValueError("Unknown fixture reply mode.")
        if not 1 <= end_after_turns <= MAX_TURNS or not 1 <= touch_budget <= 30:
            raise ValueError("Fixture limits must fit within the practice simulator limits.")
        self._reply_mode = reply_mode
        self._end_after_turns = end_after_turns
        self._touch_budget = touch_budget
        self._timeout_after_acceptance = timeout_after_acceptance
        self._sessions = {}
        self._conversations = {}
        self._receipts = {}
        self._lock = Lock()

    def research(self, target):
        if target != MOCK_TARGET:
            raise HTTPException(404, "Unknown mock target.")
        return {
            "id": MOCK_TARGET,
            "name": "Practice QA Assistant",
            "role": "Fictional simulator test persona",
            "message_channels": ["email"],
            "live_channels": ["live_chat"],
        }

    def session(self, session_id=None):
        with self._lock:
            if session_id is None:
                session_id = f"qa_mock_{uuid4().hex}"
                self._sessions[session_id] = {
                    "session_id": session_id,
                    "status": "active",
                    "scenario_id": "practice",
                    "touch_budget": self._touch_budget,
                    "touches_used": 0,
                }
            return dict(self._get_session(session_id))

    def _get_session(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Unknown mock practice session.")
        return session

    def execute(self, action, session_id, target, channel, text, conversation_id, key):
        profile = self.research(target)
        if not key:
            raise HTTPException(422, "An idempotency key is required.")
        if action not in ("send_message", "start_conversation", "continue_conversation", "end_conversation"):
            raise HTTPException(422, "Unknown mock simulator action.")
        if action in ("send_message", "continue_conversation") and (
            not isinstance(text, str) or not text.strip() or len(text) > 4000
        ):
            raise HTTPException(422, "Mock simulator text must contain 1 to 4000 characters.")

        request = (action, session_id, target, channel, text, conversation_id)
        with self._lock:
            saved = self._receipts.get(key)
            if saved is not None:
                original_request, response = saved
                if original_request != request:
                    raise HTTPException(409, "Idempotency key is already in use.")
                return deepcopy(response)

            session = self._get_session(session_id)
            if action in ("send_message", "start_conversation"):
                allowed_channels = profile["message_channels"] if action == "send_message" else profile["live_channels"]
                if channel not in allowed_channels:
                    raise HTTPException(422, "Channel is not available for this mock action.")
                response = self._contact(action, session, target)
            else:
                response = self._conversation_action(action, session_id, target, channel, conversation_id)

            response["touches_used"] = session["touches_used"]
            self._receipts[key] = (request, response)
            if self._timeout_after_acceptance == action:
                self._timeout_after_acceptance = None
                raise HTTPException(504, "Synthetic timeout after the mock action was accepted.")
            return deepcopy(response)

    def _contact(self, action, session, target):
        if session["touches_used"] >= session["touch_budget"]:
            raise HTTPException(409, "Mock practice session has no touches left.")
        session["touches_used"] += 1
        session_id = session["session_id"]
        if action == "send_message":
            message_id = f"mock_msg_{uuid4().hex}"
            events = []
            if self._reply_mode == "reply":
                events.append({
                    "id": f"mock_event_{uuid4().hex}",
                    "type": "reply",
                    "channel": "email",
                    "in_reply_to": message_id,
                    "body": "Thanks. How can I help with this practice check?",
                })
            return {"message_id": message_id, "status": "sent", "events": events}

        for conversation in self._conversations.values():
            if conversation["session_id"] == session_id and conversation["target"] == target:
                conversation["ended"] = True
        conversation_id = f"mock_call_{uuid4().hex}"
        self._conversations[conversation_id] = {
            "session_id": session_id, "target": target, "turns_used": 0, "ended": False,
        }
        return {
            "conversation_id": conversation_id,
            "status": "answered",
            "persona_says": "Hello. This is the fictional practice assistant.",
            "turns_used": 0,
            "ended": False,
            "events": [],
        }

    def _conversation_action(self, action, session_id, target, channel, conversation_id):
        conversation = self._conversations.get(conversation_id)
        if conversation is None or (conversation["session_id"], conversation["target"]) != (session_id, target):
            raise HTTPException(404, "Unknown mock conversation in this session.")
        if channel not in (None, "live_chat"):
            raise HTTPException(422, "Channel does not match the mock conversation.")

        persona_says = None
        if action == "end_conversation":
            conversation["ended"] = True
        else:
            if conversation["ended"] or conversation["turns_used"] >= MAX_TURNS:
                raise HTTPException(409, "Mock conversation has already ended.")
            conversation["turns_used"] += 1
            conversation["ended"] = conversation["turns_used"] >= self._end_after_turns
            if self._reply_mode == "reply":
                persona_says = (
                    "Thanks. That completes this practice check."
                    if conversation["ended"] else "I can help with general questions about this practice session."
                )
        return {
            "conversation_id": conversation_id,
            "status": "ended" if conversation["ended"] else "answered",
            "persona_says": persona_says,
            "turns_used": conversation["turns_used"],
            "ended": conversation["ended"],
            "events": [],
        }
