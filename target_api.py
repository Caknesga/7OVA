"""
target_api.py — thin, reusable client for the revel8 Target API.

Run this locally (Claude's sandbox has no network access to call the real
API) — import it in your main.py like:

    from target_api import TargetAPI

    api = TargetAPI()                      # reads TEAM_KEY from env
    session = api.new_session()
    sid = session["session_id"]

    result = api.send_message(
        sid, to="julius", channel="email",
        from_name="Laura Fend", from_address="laura.fend@deeptechforum-events.de",
        subject="...", body="...",
    )
    print(result["events"])

Everything below mirrors the API docs 1:1 (field names, error codes, retry
rules) so you're not re-deriving them from the PDF every time.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import requests

DEFAULT_BASE_URL = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"

# Codes the docs say are safe to retry after a short backoff.
RETRYABLE_STATUS = {429, 502, 503, 504}


class TargetAPIError(Exception):
    """Raised for any non-2xx response that isn't retried away."""

    def __init__(self, status_code: int, body: Any, request_desc: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"{status_code} on {request_desc}: {body}")


@dataclass
class Session:
    session_id: str
    scenario_id: str
    touch_budget: int
    touches_used: int
    status: str
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            session_id=data["session_id"],
            scenario_id=data.get("scenario_id", ""),
            touch_budget=data.get("touch_budget", 0),
            touches_used=data.get("touches_used", 0),
            status=data.get("status", ""),
            raw=data,
        )


class TargetAPI:
    def __init__(
        self,
        team_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 4,
        retry_backoff: float = 3.0,
        timeout: int = 120,
    ):
        self.team_key = team_key or os.environ.get("TEAM_KEY")
        if not self.team_key:
            raise RuntimeError(
                "No team key. Pass team_key=... or set the TEAM_KEY env var."
            )
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {self.team_key}"}

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {**self._headers, **(extra_headers or {})}
        desc = f"{method} {path}"

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body if files is None else None,
                    data=json_body if files is not None else None,
                    files=files,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout:
                if attempt > self.max_retries:
                    raise TargetAPIError(0, "request timed out repeatedly", desc)
                time.sleep(self.retry_backoff * attempt)
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                time.sleep(self.retry_backoff * attempt)
                continue

            if not resp.ok:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                raise TargetAPIError(resp.status_code, body, desc)

            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

    @staticmethod
    def _new_key() -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------ #
    # scenarios / channels (reference data)
    # ------------------------------------------------------------------ #
    def list_scenarios(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/scenarios")

    def list_channels(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/channels")

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #
    def new_session(self, scenario_id: str = "practice") -> Session:
        data = self._request("POST", "/sessions", {"scenario_id": scenario_id})
        return Session.from_json(data)

    def get_session(self, session_id: str) -> Session:
        data = self._request("GET", f"/sessions/{session_id}")
        return Session.from_json(data)

    def list_sessions(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/sessions")

    def get_events(self, session_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/sessions/{session_id}/events").get("events", [])

    def end_session(self, session_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/sessions/{session_id}/end")

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        session_id: str,
        to: str,
        channel: str,
        from_name: Optional[str] = None,
        from_address: Optional[str] = None,
        body: str = "",
        subject: Optional[str] = None,
        attachments: Optional[List[Union[str, Dict[str, Any]]]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        attachments: list of either
            "a_2"                                            (uploaded asset id)
            {"filename": "...", "transcript": "..."}         (audio)
            {"filename": "..."}                              (any other file, name only)
        """
        payload: Dict[str, Any] = {"to": to, "channel": channel, "body": body}
        if from_name or from_address:
            payload["from"] = {"name": from_name, "address": from_address}
        if subject:
            payload["subject"] = subject
        if attachments:
            payload["attachments"] = attachments

        key = idempotency_key or self._new_key()
        return self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            payload,
            extra_headers={"Idempotency-Key": key},
        )

    # ------------------------------------------------------------------ #
    # landing pages
    # ------------------------------------------------------------------ #
    def register_page(
        self,
        session_id: str,
        url: str,
        title: str,
        html: Optional[str] = None,
        text: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"url": url, "title": title, "fields": fields or []}
        if html:
            payload["html"] = html
        if text:
            payload["text"] = text
        return self._request("POST", f"/sessions/{session_id}/pages", payload)

    # ------------------------------------------------------------------ #
    # assets
    # ------------------------------------------------------------------ #
    def upload_asset(self, path: str) -> Dict[str, Any]:
        with open(path, "rb") as f:
            return self._request(
                "POST", "/assets", files={"file": (os.path.basename(path), f)}
            )

    # ------------------------------------------------------------------ #
    # conversations (live: phone_call, video_call, teams_call, live_chat)
    # ------------------------------------------------------------------ #
    def start_conversation(
        self,
        session_id: str,
        to: str,
        channel: str,
        from_name: Optional[str] = None,
        from_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"to": to, "channel": channel}
        if from_name or from_address:
            payload["from"] = {"name": from_name, "address": from_address}
        return self._request("POST", f"/sessions/{session_id}/conversations", payload)

    def send_turn(
        self,
        conversation_id: str,
        say: str,
        attachments: Optional[List[Union[str, Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"say": say}
        if attachments:
            payload["attachments"] = attachments
        return self._request(
            "POST", f"/conversations/{conversation_id}/turns", payload
        )

    def end_conversation(self, conversation_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/conversations/{conversation_id}/end")

    def run_conversation(self, session_id: str, to: str, channel: str, next_line_fn,
                          from_name: Optional[str] = None,
                          from_address: Optional[str] = None,
                          max_turns: int = 10) -> Dict[str, Any]:
        """
        Convenience loop for a live channel. next_line_fn(heard: str | None) -> str | None
        is YOUR agent's logic for what to say next; return None to hang up.
        Returns the final conversation state dict plus a 'transcript' list.
        """
        conv = self.start_conversation(session_id, to, channel, from_name, from_address)
        transcript = []
        heard = conv.get("persona_says")
        if heard:
            transcript.append({"role": "persona", "text": heard})

        turns = 0
        while conv.get("status") == "answered" and turns < max_turns:
            line = next_line_fn(heard)
            if line is None:
                break
            turn = self.send_turn(conv["conversation_id"], line)
            transcript.append({"role": "agent", "text": line})
            turns += 1
            heard = turn.get("persona_says")
            if heard:
                transcript.append({"role": "persona", "text": heard})
            if turn.get("ended"):
                conv["status"] = "ended"
                conv["final_turn"] = turn
                break
        else:
            conv["status"] = conv.get("status", "answered")

        conv["transcript"] = transcript
        return conv
