"""Async HTTP client for revel8's *simulated* Target API.

Only calls the supplied simulator endpoint: it does not deliver real email,
phone calls, messages, or social-network contacts.

Requires: httpx, python-dotenv, and communication.schemas (Pydantic v2).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .schemas import ConversationStartRequest, ConversationTurnRequest, MessageRequest


BASE_URL = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class TargetAPIError(Exception):
    """Non-success API response or a failed network request."""

    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class TargetAPIClient:
    """One async client per application process.

    Use ``async with TargetAPIClient() as api`` to close connections reliably.
    Send requests sequentially within each session, as required by the API.
    """

    def __init__(
        self,
        team_key: str | None = None,
        *,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        load_dotenv(root / ".env")

        key = (team_key or os.getenv("TEAM_KEY") or "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if not key:
            raise ValueError("TEAM_KEY is missing; put the simulator team key in .env")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self.max_attempts = max_attempts
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(120.0, connect=15.0),
            transport=transport,
        )

    async def __aenter__(self) -> "TargetAPIClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retry_network_errors: bool = True,
    ) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        error: Exception | None = None

        for attempt in range(self.max_attempts):
            response: httpx.Response | None = None
            try:
                response = await self._http.request(
                    method, path, json=payload, headers=headers
                )
            except httpx.RequestError as exc:
                error = exc
                if not retry_network_errors:
                    raise TargetAPIError(
                        "Network failure on a request without documented idempotency. "
                        "Check the session list before trying again."
                    ) from exc
                if attempt == self.max_attempts - 1:
                    break
            else:
                if 200 <= response.status_code < 300:
                    if not response.content:
                        return {}
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise TargetAPIError(
                            "Simulator returned invalid JSON",
                            status_code=response.status_code,
                        ) from exc

                if response.status_code not in RETRYABLE_STATUSES:
                    raise TargetAPIError(
                        f"Target API returned HTTP {response.status_code}: "
                        f"{response.text[:500]}",
                        status_code=response.status_code,
                    )
                error = TargetAPIError(
                    f"Target API returned HTTP {response.status_code}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                )

            if attempt < self.max_attempts - 1:
                # For 429 honor numeric Retry-After when supplied, capped at 10s.
                delay = min(2 ** attempt, 10)
                if response is not None and response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = min(max(float(retry_after), 0), 10)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)

        raise TargetAPIError(
            f"Target API request failed after {self.max_attempts} attempts: {error}",
            status_code=getattr(error, "status_code", None),
        ) from error

    # Discovery / session state (read-only; no touches spent)
    async def get_scenarios(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/scenarios")

    async def get_channels(self) -> Any:
        # The documentation does not specify a fixed response shape.
        return await self._request("GET", "/channels")

    async def list_sessions(self) -> Any:
        return await self._request("GET", "/sessions")

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}")

    async def get_events(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}/events")

    # Session lifecycle
    async def create_session(self, scenario_id: str = "practice") -> dict[str, Any]:
        return await self._request(
            "POST", "/sessions", payload={"scenario_id": scenario_id},
            retry_network_errors=False,
        )

    async def end_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/sessions/{session_id}/end")

    # Communication operations. Keep the SAME key when retrying the SAME action.
    async def send_message(
        self,
        session_id: str,
        message: MessageRequest,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            payload=message.model_dump(by_alias=True, exclude_none=True),
            idempotency_key=request_id or str(uuid.uuid4()),
        )

    async def start_conversation(
        self,
        session_id: str,
        conversation: ConversationStartRequest,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/conversations",
            payload=conversation.model_dump(by_alias=True, exclude_none=True),
            idempotency_key=request_id or str(uuid.uuid4()),
        )

    async def send_turn(
        self,
        conversation_id: str,
        turn: ConversationTurnRequest,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/conversations/{conversation_id}/turns",
            payload=turn.model_dump(exclude_none=True),
            idempotency_key=request_id or str(uuid.uuid4()),
        )

    async def end_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/conversations/{conversation_id}/end")
