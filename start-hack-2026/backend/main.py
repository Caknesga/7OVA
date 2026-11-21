import hashlib
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints

ROOT = Path(__file__).resolve().parents[1]
API = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"
PRACTICE = "practice"
LIVE_CHANNELS = {"phone_call", "video_call", "teams_call", "live_chat"}
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
OperationKey = Annotated[UUID, Header(alias="Idempotency-Key")]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Identifier
    channel: Identifier
    subject: Annotated[str, StringConstraints(max_length=300)] = ""
    body: MessageText


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
            if item not in LIVE_CHANNELS:
                names.append(item)
    if value and not names:
        raise HTTPException(502, "No message channels could be read from the simulator.")
    return list(dict.fromkeys(names))


def create_app(team_key=None, db_path=None, transport=None):
    load_dotenv(ROOT / ".env")
    secret = os.environ.get("TEAM_KEY", "") if team_key is None else team_key
    database = Path(db_path or os.environ.get("DATABASE_PATH", ROOT / "data/history.sqlite3"))
    write_lock = Lock()

    @contextmanager
    def connect():
        with closing(sqlite3.connect(database)) as connection, connection:
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
            """)
        with httpx.Client(
            base_url=API + "/", timeout=120, follow_redirects=False,
            trust_env=False, transport=transport,
            headers={"Authorization": f"Bearer {secret}"},
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(title="START Hack Practice Console", lifespan=lifespan)

    def upstream(path, *, payload=None, key=None):
        if not secret:
            raise HTTPException(503, "Set TEAM_KEY in the backend environment, then restart it.")
        try:
            response = app.state.client.request(
                "GET" if payload is None else "POST", path, json=payload,
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
                ON CONFLICT(session, kind, id) DO UPDATE SET payload = excluded.payload""",
                (sid, event_id, json.dumps(event)))

    def operation(key, path, payload, save=None):
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
                db.execute("INSERT OR IGNORE INTO operations VALUES (?, ?, NULL)", (key, fingerprint))
            try:
                result = upstream(path, payload=payload, key=key)
            except HTTPException as error:
                if save is None and error.status_code in {400, 401, 403, 404, 409, 422, 429}:
                    with connect() as db:
                        db.execute("DELETE FROM operations WHERE id = ?", (key,))
                raise

            if not isinstance(result, dict):
                raise HTTPException(502, "Unexpected simulator write response.")
            with connect() as db:
                if save is not None:
                    save(db, result)
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
        if message.channel in LIVE_CHANNELS:
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

    return app


app = create_app()
