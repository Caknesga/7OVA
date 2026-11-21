import hashlib
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from collections import deque
from threading import Lock, RLock
from time import monotonic
from typing import Annotated
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, ValidationError

from . import conversations as live
from .qa import MESSAGE_CHANNELS, PHRASES, create_qa

ROOT = Path(__file__).resolve().parents[1]
API = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"
PRACTICE = "practice"
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
OperationKey = Annotated[UUID, Header(alias="Idempotency-Key")]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Identifier
    channel: Identifier
    subject: Annotated[str, StringConstraints(max_length=300)] = ""
    body: MessageText


class StartConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Identifier
    channel: live.LiveChannel


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    say: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class StartReceipt(BaseModel):
    conversation_id: Identifier
    status: str
    persona_says: str | None = None
    ended: StrictBool = False
    turns_used: Annotated[int, Field(strict=True, ge=0, le=live.MAX_TURNS)] = 0
    touches_used: Annotated[int, Field(strict=True, ge=0)] | None = None
    events: list[dict] = Field(default_factory=list)


class TurnReceipt(BaseModel):
    conversation_id: Identifier | None = None
    status: str | None = None
    persona_says: str | None = None
    ended: StrictBool
    turns_used: Annotated[int, Field(strict=True, ge=1, le=live.MAX_TURNS)]
    events: list[dict]


def parse_receipt(model, result):
    try:
        return model.model_validate(result)
    except ValidationError:
        raise HTTPException(502, "Unexpected simulator conversation response.") from None


def records(value, key):
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HTTPException(502, f"Unexpected simulator {key} response.")
    return value


def message_channels(value):
    if isinstance(value, dict):
        for key in ("message", "messages", "messaging", "message_channels", "channels"):
            if key in value:
                return message_channels(value[key])
        raise HTTPException(502, "Unexpected simulator channels response.")
    if not isinstance(value, list):
        raise HTTPException(502, "Unexpected simulator channels response.")

    names = []
    for item in value:
        if isinstance(item, dict):
            if item.get("kind", item.get("type")) in {"live", "conversation"}:
                continue
            item = item.get("id") or item.get("channel") or item.get("name")
        if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]*", item):
            if item not in live.LIVE_CHANNELS:
                names.append(item)
    if value and not names:
        raise HTTPException(502, "No message channels could be read from the simulator.")
    return list(dict.fromkeys(names))


def create_app(team_key=None, db_path=None, transport=None, *, qa_options=None):
    load_dotenv(ROOT / ".env")
    secret = os.environ.get("TEAM_KEY", "") if team_key is None else team_key
    database = Path(db_path or os.environ.get("DATABASE_PATH", ROOT / "data/history.sqlite3"))
    write_lock = RLock()
    upstream_lock = Lock()
    request_times = deque()

    @contextmanager
    def connect():
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    @asynccontextmanager
    async def lifespan(app):
        database.parent.mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history (
                    session TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(session, kind, id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT
                );
            """ + live.SCHEMA)
        with httpx.Client(
            base_url=API + "/", timeout=120, follow_redirects=False,
            trust_env=False, transport=transport,
            headers={"Authorization": f"Bearer {secret}"},
        ) as client:
            app.state.client = client
            request_times.clear()
            async with qa_lifespan():
                yield

    app = FastAPI(title="START Hack Practice Console", lifespan=lifespan)

    def upstream(path, *, payload=None, key=None, method=None, allow_empty=False):
        if not secret:
            raise HTTPException(503, "Set TEAM_KEY in the backend environment, then restart it.")
        try:
            with upstream_lock:
                now = monotonic()
                while request_times and now - request_times[0] >= 60:
                    request_times.popleft()
                if len(request_times) >= 60:
                    delay = int(60 - (now - request_times[0])) + 1
                    raise HTTPException(429, "Simulator request limit reached.",
                                        headers={"Retry-After": str(delay)})
                request_times.append(now)
                response = app.state.client.request(
                    method or ("GET" if payload is None else "POST"), path, json=payload,
                    headers={"Idempotency-Key": key} if key else None,
                )
        except httpx.TimeoutException:
            raise HTTPException(504, "Simulator timed out after 120 seconds.") from None
        except httpx.RequestError:
            raise HTTPException(502, "Could not reach the simulator.") from None
        if not response.is_success:
            messages = {
                400: "The simulator rejected the request.",
                401: "The simulator rejected TEAM_KEY. Check the backend configuration.",
                403: "The simulator refused access to this scenario.",
                404: "The simulator could not find this resource.",
                409: "Session closed or no touches remain. Refresh session details.",
                422: "The simulator could not process this message. Edit it before trying again.",
                429: "Simulator rate limit reached. Wait a few seconds before retrying.",
                503: "Simulator temporarily unavailable. Wait a few seconds before retrying.",
            }
            status = response.status_code if response.status_code in messages or response.status_code in {502, 504} else 502
            retry_after = response.headers.get("Retry-After")
            raise HTTPException(
                status, messages.get(status, "Unexpected response from the simulator."),
                headers={"Retry-After": retry_after} if retry_after and status in {429, 503} else None,
            )
        if allow_empty and not response.content.strip():
            return {}
        try:
            # Never relay an echoed key, even inside a successful simulator payload.
            data = response.json()
            if secret in json.dumps(data, ensure_ascii=False):
                raise ValueError("Unexpected credential in response")
            return data
        except ValueError:
            raise HTTPException(502, "Invalid simulator response.") from None

    def practice_session(sid):
        session = upstream(f"sessions/{sid}")
        if not isinstance(session, dict):
            raise HTTPException(502, "Unexpected simulator session response.")
        if session.get("scenario_id") != PRACTICE or session.get("session_id") != sid:
            raise HTTPException(404, "Practice session not found.")
        return session

    def store_events(db, sid, events):
        for event in events:
            event_id = event.get("id")
            if not isinstance(event_id, str) or not event_id:
                raise HTTPException(502, "Simulator returned an event without an ID.")
            db.execute("""INSERT INTO history VALUES (?, 'event', ?, ?)
                ON CONFLICT(session, kind, id) DO UPDATE SET payload =
                    CASE WHEN json_extract(excluded.payload, '$.conversation_id') IS NULL
                            AND json_extract(history.payload, '$.conversation_id') IS NOT NULL
                         THEN json_set(excluded.payload, '$.conversation_id',
                                       json_extract(history.payload, '$.conversation_id'))
                         ELSE excluded.payload END""",
                (sid, event_id, json.dumps(event)))

    def save_conversation_events(db, sid, cid, events):
        linked = []
        for event in events:
            if event.get("conversation_id") not in (None, cid):
                raise HTTPException(502, "Simulator returned an event for another conversation.")
            linked.append({**event, "conversation_id": cid})
        store_events(db, sid, linked)
        return linked

    def operation(key, path, payload, save=None, *, validate=None, close_on_conflict=None):
        key = str(key)
        fingerprint = hashlib.sha256(json.dumps([path, payload], sort_keys=True).encode()).hexdigest()
        with write_lock:
            with connect() as db:
                row = db.execute("SELECT fingerprint, result FROM operations WHERE id = ?", (key,)).fetchone()
                if row and row[0] != fingerprint:
                    raise HTTPException(409, "Use a new idempotency key for an edited request.")
                if row and row[1] is not None:
                    return json.loads(row[1])
                if row and save is None:
                    raise HTTPException(409, "Session creation may have succeeded. Refresh the session list before creating another.")
            if validate is not None:
                validate()
            with connect() as db:
                db.execute("INSERT OR IGNORE INTO operations VALUES (?, ?, NULL)", (key, fingerprint))
            try:
                result = upstream(path, payload=payload, key=key)
            except HTTPException as error:
                if close_on_conflict is not None and error.status_code == 409:
                    with connect() as db:
                        live.close(db, close_on_conflict)
                    raise HTTPException(409, "The simulator has closed this conversation.") from None
                if save is None and error.status_code in {400, 401, 403, 404, 409, 422, 429}:
                    with connect() as db:
                        db.execute("DELETE FROM operations WHERE id = ?", (key,))
                raise

            if not isinstance(result, dict):
                raise HTTPException(502, "Unexpected simulator write response.")
            with connect() as db:
                if save is not None:
                    saved = save(db, result)
                    if saved is not None:
                        result = saved
                elif result.get("scenario_id") != PRACTICE or not result.get("session_id"):
                    raise HTTPException(502, "Unexpected practice session response.")
                db.execute("UPDATE operations SET result = ? WHERE id = ?", (json.dumps(result), key))
            return result

    @app.get("/api/status")
    def status():
        return {"configured": bool(secret), "scenario": PRACTICE}

    @app.get("/api/scenarios")
    def scenarios():
        return [item for item in records(upstream("scenarios"), "scenarios") if item.get("id") == PRACTICE]

    @app.get("/api/channels")
    def channels():
        return message_channels(upstream("channels"))

    @app.get("/api/sessions")
    def sessions():
        return [session for session in records(upstream("sessions"), "sessions")
                if session.get("scenario_id") == PRACTICE]

    @app.post("/api/sessions", status_code=201)
    def create_session(key: OperationKey):
        if not secret:
            raise HTTPException(503, "Set TEAM_KEY in the backend environment, then restart it.")
        return operation(key, "sessions", {"scenario_id": PRACTICE})

    @app.get("/api/sessions/{sid}")
    def detail(sid: Identifier):
        return practice_session(sid)

    @app.get("/api/sessions/{sid}/events")
    def events(sid: Identifier):
        practice_session(sid)
        events = records(upstream(f"sessions/{sid}/events"), "events")
        with connect() as db:
            store_events(db, sid, events)
        return events

    @app.get("/api/sessions/{sid}/history")
    def history(sid: Identifier):
        with connect() as db:
            rows = db.execute("SELECT kind, payload FROM history WHERE session = ? ORDER BY rowid", (sid,))
            result = {"messages": [], "events": []}
            for kind, payload in rows:
                result[kind + "s"].append(json.loads(payload))
        return result

    @app.post("/api/sessions/{sid}/messages")
    def send(sid: Identifier, message: Message, key: OperationKey):
        practice_session(sid)
        if message.channel in live.LIVE_CHANNELS:
            raise HTTPException(422, "Select a message channel.")
        payload = message.model_dump()
        if secret in json.dumps(payload, ensure_ascii=False):
            raise HTTPException(422, "Remove the team key from the message.")
        if not payload["subject"]:
            del payload["subject"]

        def save(db, result):
            mid = result.get("message_id")
            if not isinstance(mid, str) or not mid:
                raise HTTPException(502, "Simulator returned a message without an ID.")
            db.execute("INSERT OR IGNORE INTO history VALUES (?, 'message', ?, ?)",
                       (sid, mid, json.dumps({**payload, "message_id": mid})))
            store_events(db, sid, records(result, "events"))

        return operation(key, f"sessions/{sid}/messages", payload, save)

    @app.get("/api/sessions/{sid}/conversations")
    def list_live_conversations(sid: Identifier):
        with connect() as db:
            db.execute("BEGIN")
            return live.list_for_session(db, sid)

    @app.post("/api/sessions/{sid}/conversations", status_code=201)
    def start_live_conversation(sid: Identifier, start: StartConversation, key: OperationKey):
        def save(db, result):
            receipt = parse_receipt(StartReceipt, result)
            receipt.ended = receipt.ended or receipt.status == "ended"
            if receipt.ended:
                receipt.status = "ended"
            live.record_start(db, sid, start.to, start.channel, receipt)
            receipt.events = save_conversation_events(db, sid, receipt.conversation_id, receipt.events)
            return receipt.model_dump()

        return operation(key, f"sessions/{sid}/conversations", start.model_dump(), save,
                         validate=lambda: practice_session(sid))

    @app.post("/api/conversations/{cid}/turns")
    def live_turn(cid: Identifier, turn: ConversationTurn, key: OperationKey):
        if secret and secret in turn.say:
            raise HTTPException(422, "Remove the team key from the turn.")

        def validate():
            with connect() as db:
                conversation = live.get(db, cid)
            if conversation["ended"]:
                raise HTTPException(409, "This conversation has ended.")
            if conversation["turns_used"] >= live.MAX_TURNS:
                raise HTTPException(409, "This conversation has reached its 10-turn limit.")

        def save(db, result):
            conversation = live.get(db, cid)
            receipt = parse_receipt(TurnReceipt, result)
            if receipt.conversation_id not in (None, cid):
                raise HTTPException(502, "Simulator replied for another conversation.")
            receipt.conversation_id = cid
            receipt.ended = receipt.ended or receipt.status == "ended"
            receipt.status = "ended" if receipt.ended else (receipt.status or conversation["status"])
            live.record_turn(db, cid, turn.say, receipt)
            receipt.events = save_conversation_events(db, conversation["session"], cid, receipt.events)
            return receipt.model_dump()

        return operation(key, f"conversations/{cid}/turns", turn.model_dump(), save,
                         validate=validate, close_on_conflict=cid)

    @app.post("/api/conversations/{cid}/end")
    def end_live_conversation(cid: Identifier, _key: OperationKey):
        with write_lock, connect() as db:
            conversation = live.get(db, cid)
            events = []
            if not conversation["ended"]:
                try:
                    response = upstream(f"conversations/{cid}/end", method="POST", allow_empty=True)
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
                    response = {}
                if not isinstance(response, dict):
                    raise HTTPException(502, "Unexpected simulator conversation end response.")
                events = save_conversation_events(
                    db, conversation["session"], cid, records(response.get("events", []), "events"),
                )
                live.close(db, cid)

            return {"conversation_id": cid, "status": "ended", "ended": True,
                    "turns_used": conversation["turns_used"], "events": events}

    class PracticeQA:
        available = bool(secret)

        def research(self, target):
            scenario = next((item for item in scenarios() if item.get("open")), None)
            person = next((item for item in scenario.get("targets", [])
                           if item.get("id") == target), None) if scenario else None
            if person is None:
                raise HTTPException(404, "Practice QA target not found.")
            available = upstream("channels")
            named_messages = message_channels(available)
            live_entries = available
            if isinstance(available, dict):
                live_entries = next((available[key] for key in (
                    "conversation", "conversations", "live", "live_channels", "conversation_channels", "channels"
                ) if key in available), [])
            live_names = [
                entry.get("id") or entry.get("channel") or entry.get("name") if isinstance(entry, dict) else entry
                for entry in live_entries
            ] if isinstance(live_entries, list) else []
            return {"id": target, "name": str(person.get("name") or target)[:200],
                    "role": str(person.get("role") or "")[:300], "source": "revel8 practice scenario",
                    "message_channels": [name for name in MESSAGE_CHANNELS if name in named_messages],
                    "live_channels": [name for name in live.LIVE_CHANNELS if name in live_names]}

        def session(self, session_id):
            return practice_session(session_id)

        def execute(self, action, sid, target, channel, phrase, cid, key, *, cap, retry, check_stop):
            with write_lock:
                check_stop()
                if action in {"send_message", "start_conversation"} and not retry:
                    current = practice_session(sid)
                    if current["status"] != "active" or current["touches_used"] >= min(cap, current["touch_budget"]):
                        raise HTTPException(409, "Practice session limit reached.")
                check_stop()
                if action in {"send_message", "continue_conversation"} and phrase not in PHRASES.values():
                    raise HTTPException(422, "Use an approved QA phrase.")
                if action == "send_message":
                    return send(sid, Message(to=target, channel=channel, body=phrase), key)
                if action == "start_conversation":
                    return start_live_conversation(sid, StartConversation(to=target, channel=channel), key)
                with connect() as db:
                    if live.get(db, cid)["session"] != sid:
                        raise HTTPException(404, "Conversation not found in this QA session.")
                if action == "continue_conversation":
                    return live_turn(cid, ConversationTurn(say=phrase), key)
                if action == "end_conversation":
                    return end_live_conversation(cid, key)
                raise HTTPException(422, "Unsupported QA action.")

    qa_router, qa_lifespan = create_qa(connect, PracticeQA(), **(qa_options or {}))
    app.include_router(qa_router)
    return app


app = create_app()
