"""Bounded simulator QA runs. Model output is data, never a tool invocation."""
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Event
from time import monotonic
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .conversations import LIVE_CHANNELS, MAX_TURNS
from .qa_openai import ProviderError, Settings, provider
from .qa_simulator import MockSimulator

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
Key = Annotated[UUID, Header(alias="Idempotency-Key")]
MESSAGE_CHANNELS = ("email", "sms")
CONTACTS = ("send_message", "start_conversation")
PHRASES = {
    "hello": "Hello. This is an automated practice simulator test. Can you acknowledge this message?",
    "availability": "This is a practice simulator test. Can you confirm that you can read this turn?",
    "thanks": "Thank you for helping test the practice simulator.",
}
EVENT_TYPES = {"reply", "link.opened", "page.submitted", "attachment.opened"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_runs (
    id TEXT PRIMARY KEY, request TEXT NOT NULL, mode TEXT NOT NULL,
    target TEXT NOT NULL, session_id TEXT, max_touches INTEGER NOT NULL,
    model TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'START',
    status TEXT NOT NULL DEFAULT 'running', stop_requested INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT, touches_used INTEGER NOT NULL DEFAULT 0, profile TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS qa_timeline (
    seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES qa_runs(id),
    state TEXT NOT NULL, detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS qa_usage (
    seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES qa_runs(id),
    phase TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_usd REAL NOT NULL, estimated_only INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS qa_timeline_run ON qa_timeline(run_id);
CREATE INDEX IF NOT EXISTS qa_usage_run ON qa_usage(run_id);
"""
TOTALS = """SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
    COALESCE(SUM(estimated_usd), 0) AS estimated_usd FROM qa_usage"""


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["mock", "practice"]
    target: Id
    session_id: Id | None = None
    max_touches: int = Field(strict=True, ge=1, le=10)
    confirm_practice: bool = False


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["send_message", "start_conversation", "continue_conversation", "end_conversation", "stop"]
    channel: Literal["email", "sms", "phone_call", "video_call", "teams_call", "live_chat"] | None


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: Literal["hello", "availability", "thanks"]


class Evaluate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation: Literal["acknowledgement", "question", "other"]


class StopRun(Exception):
    def __init__(self, reason, status="stopped"):
        super().__init__(reason)
        self.status = status


def observation(receipt, mock):
    events = receipt.get("events") or []
    event_types = [event["type"] if event.get("type") in EVENT_TYPES else "other" for event in events]
    replies = [event.get("body") for event in events if event.get("type") == "reply"]
    replies.append(receipt.get("persona_says"))
    replies = [value for value in replies if isinstance(value, str) and value.strip()]
    ended = bool(receipt.get("ended"))
    kind = "ended" if ended else "silence"
    if events:
        kind = "observable_events"
    if replies:
        kind = "reply"
    info = {
        "observation": kind,
        "event_types": event_types,
        "ended": ended,
        "has_question_mark": any("?" in value for value in replies),
    }
    if mock:
        info["replies"] = replies
    return info


def create_qa(connect, practice, *, simulator_factory=MockSimulator, provider_factory=provider,
              settings=None, retry_delay=2):
    config_error = None
    if settings is None:
        try:
            settings = Settings.from_env()
        except ValueError as error:
            config_error = str(error)
            settings = Settings()
    router = APIRouter(prefix="/api/qa")
    stopping = Event()
    executor = None

    def log_state(db, run_id, state, detail):
        db.execute("UPDATE qa_runs SET state = ? WHERE id = ?", (state, run_id))
        db.execute("INSERT INTO qa_timeline (run_id, state, detail) VALUES (?, ?, ?)",
                   (run_id, state, json.dumps(detail)))

    def transition(run_id, state, **detail):
        with connect() as db:
            log_state(db, run_id, state, detail)

    def finish(db, run_id, status, reason):
        db.execute("UPDATE qa_runs SET status = ?, stop_reason = ? WHERE id = ?", (status, reason, run_id))
        log_state(db, run_id, "STOP", {"reason": reason, "status": status})

    @asynccontextmanager
    async def lifespan():
        nonlocal executor
        stopping.clear()
        with connect() as db:
            db.executescript(SCHEMA)
            for row in db.execute("SELECT id FROM qa_runs WHERE status = 'running'").fetchall():
                finish(db, row["id"], "stopped", "Interrupted by backend restart")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="practice-qa")
        try:
            yield
        finally:
            stopping.set()
            await asyncio.to_thread(executor.shutdown, wait=True)

    def snapshot(db, row, include_timeline=False):
        data = {key: row[key] for key in (
            "id", "mode", "target", "session_id", "model", "state", "status",
            "stop_reason", "max_touches", "touches_used", "created_at", "stop_requested",
        )}
        data["profile"] = json.loads(row["profile"]) if row["profile"] else None
        data["usage"] = dict(db.execute(TOTALS + " WHERE run_id = ?", (row["id"],)).fetchone())
        if include_timeline:
            data["timeline"] = [
                {"state": event["state"], "detail": json.loads(event["detail"]), "created_at": event["created_at"]}
                for event in db.execute("SELECT * FROM qa_timeline WHERE run_id = ? ORDER BY seq", (row["id"],))
            ]
        return data

    def get(db, run_id):
        row = db.execute("SELECT * FROM qa_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "QA run not found.")
        return row

    def check_stop(run_id):
        if stopping.is_set():
            raise StopRun("Backend shutdown")
        with connect() as db:
            if get(db, run_id)["stop_requested"]:
                raise StopRun("Stopped by user")

    def infer(run_id, llm, mock, phase, context, schema):
        check_stop(run_id)
        reserved = settings.reserve(phase, context, schema, mock=mock)
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not mock:
                total = db.execute(TOTALS).fetchone()["estimated_usd"]
                spent = db.execute(TOTALS + " WHERE run_id = ?", (run_id,)).fetchone()["estimated_usd"]
                if total + reserved > settings.budget_usd or total >= settings.budget_usd:
                    raise StopRun("Estimated project budget reached")
                if spent + reserved > settings.max_run_cost_usd or spent >= settings.max_run_cost_usd:
                    raise StopRun("Estimated per-run budget reached")
            seq = db.execute(
                "INSERT INTO qa_usage (run_id, phase, estimated_usd) VALUES (?, ?, ?)",
                (run_id, phase, reserved),
            ).lastrowid

        def settle(usage, cost, estimated_only):
            with connect() as db:
                db.execute("""UPDATE qa_usage SET input_tokens=?, output_tokens=?, cached_tokens=?,
                    estimated_usd=?, estimated_only=? WHERE seq=?""",
                    (usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                     usage.get("cached_tokens", 0), cost, estimated_only, seq))

        try:
            result, usage = llm(phase, context, schema)
        except ProviderError as error:
            if error.usage is not None:
                settle(error.usage, settings.estimate(error.usage, mock=mock), False)
            elif not error.may_have_cost:
                settle({}, 0, False)
            raise StopRun(str(error), "failed") from None
        settle(usage, settings.estimate(usage, mock=mock), False)
        check_stop(run_id)
        return result

    def communicate(run_id, simulator, action, sid, target, channel, phrase, cid, step, cap):
        key = str(uuid5(NAMESPACE_URL, f"revel8-qa/{run_id}/{step}"))
        check_stop(run_id)
        transition(run_id, "COMMUNICATE", action=action, channel=channel, text=phrase, conversation_id=cid)
        for attempt in range(2):
            check_stop(run_id)
            try:
                args = (action, sid, target, channel, phrase, cid, key)
                if simulator is practice:
                    return simulator.execute(*args, cap=cap, retry=attempt > 0,
                                             check_stop=lambda: check_stop(run_id))
                return simulator.execute(*args)
            except HTTPException as error:
                if attempt or error.status_code not in {429, 502, 503, 504}:
                    raise StopRun(f"Simulator request failed (HTTP {error.status_code}).", "failed") from None
                transition(run_id, "COMMUNICATE", action=action, retry=True, http_status=error.status_code)
                delay = (error.headers or {}).get("Retry-After", "")
                wait = max(retry_delay, int(delay)) if delay.isdecimal() else retry_delay
                if wait > 60:
                    raise StopRun("Simulator rate limit requires a longer wait.") from None
                deadline = monotonic() + wait
                while (remaining := deadline - monotonic()) > 0:
                    stopping.wait(min(remaining, 0.25))
                    check_stop(run_id)

    def run_workflow(run_id, request):
        mock = request.mode == "mock"
        simulator = simulator_factory() if mock else practice
        check_stop(run_id)
        transition(run_id, "RESEARCH", source="synthetic fixture" if mock else "revel8 practice scenario")
        profile = simulator.research(request.target)
        session = simulator.session(request.session_id)
        if session["scenario_id"] != "practice" or session["status"] != "active":
            raise StopRun("An active practice session is required.", "failed")
        sid = session["session_id"]
        baseline = session["touches_used"]
        cap = min(session["touch_budget"], baseline + request.max_touches)
        messages = sorted(set(profile["message_channels"]).intersection(MESSAGE_CHANNELS))
        live = sorted(set(profile["live_channels"]).intersection(LIVE_CHANNELS))
        with connect() as db:
            db.execute("UPDATE qa_runs SET session_id=?, profile=? WHERE id=?", (sid, json.dumps(profile), run_id))
            log_state(db, run_id, "RESEARCH", {"profile": profile, "remaining_touches": max(0, cap - baseline)})
        llm = provider_factory(settings, mock)
        cid, turns, touches = None, 0, baseline
        last_action, last_observation = "none", "none"
        for step in range(1, 31):
            if cid:
                allowed = ["end_conversation"]
                if turns < MAX_TURNS:
                    allowed.insert(0, "continue_conversation")
            else:
                allowed = ["stop"]
                if touches < cap:
                    if messages:
                        allowed.append("send_message")
                    if live:
                        allowed.append("start_conversation")
            if allowed == ["stop"]:
                return "Touch limit reached" if touches >= cap else "No supported QA channel"
            transition(run_id, "PLAN", allowed=allowed)
            context = {"allowed": allowed, "message_channels": messages, "live_channels": live,
                       "last_observation": last_observation, "last_action": last_action,
                       "has_conversation": bool(cid)}
            plan = infer(run_id, llm, mock, "plan", context, Plan)
            action, channel = plan.action, plan.channel
            permitted_channels = messages if action == "send_message" else live if action == "start_conversation" else [None]
            if action not in allowed or channel not in permitted_channels:
                raise StopRun("Planner returned an action outside the allowed QA options.", "failed")
            transition(run_id, "PLAN", decision=action, channel=channel)
            if action == "stop":
                return "Planner finished the QA check"
            phrase = None
            if action in {"send_message", "continue_conversation"}:
                transition(run_id, "GENERATE", action=action)
                choice = infer(run_id, llm, mock, "generate", {"action": action}, Generate)
                phrase = PHRASES[choice.phrase]
            receipt = communicate(run_id, simulator, action, sid, request.target, channel, phrase, cid, step, cap)
            if action in CONTACTS:
                used = receipt.get("touches_used")
                if type(used) is not int or used <= touches:
                    raise StopRun("Simulator returned an invalid touch counter.", "failed")
                touches = used
            if action == "start_conversation":
                cid, turns = receipt["conversation_id"], 0
            if action == "continue_conversation":
                turns = receipt["turns_used"]
            info = observation(receipt, mock)
            with connect() as db:
                db.execute("UPDATE qa_runs SET touches_used=? WHERE id=?", (touches - baseline, run_id))
                log_state(db, run_id, "EVALUATE", {
                    **info, "action": action, "message_id": receipt.get("message_id"),
                    "conversation_id": cid, "turns_used": turns, "remaining_touches": max(0, cap - touches),
                })
            if info["observation"] == "reply":
                assessment = infer(run_id, llm, mock, "evaluate",
                                   {"has_question_mark": info["has_question_mark"], "has_reply": True,
                                    "ended": info["ended"]}, Evaluate)
                info["classification"] = assessment.observation
            transition(run_id, "MEMORY", classification=info.get("classification"),
                       observation=info["observation"], action=action)
            if action == "end_conversation" or info["ended"]:
                cid = None
            last_action = action
            last_observation = "events" if info["observation"] == "observable_events" else info["observation"]
            transition(run_id, "CONTINUE", last_action=last_action)
        if cid:
            communicate(run_id, simulator, "end_conversation", sid, request.target, None, None, cid, 31, cap)
        return "Interaction step limit reached"

    def execute_run(run_id, request):
        try:
            reason, status = run_workflow(run_id, request), "completed"
        except StopRun as error:
            reason, status = str(error), error.status
        except HTTPException as error:
            reason, status = f"Practice data unavailable (HTTP {error.status_code}).", "failed"
        except Exception:
            logging.getLogger(__name__).error("QA run failed unexpectedly (%s)", run_id)
            reason, status = "Unexpected QA run error.", "failed"
        with connect() as db:
            finish(db, run_id, status, reason)

    @router.get("/config")
    def config():
        with connect() as db:
            total = db.execute(TOTALS).fetchone()["estimated_usd"]
        return {"model": settings.model, "real_available": settings.available and practice.available,
                "configuration_error": config_error or settings.configuration_error,
                "budget_usd": settings.budget_usd, "soft_limit_usd": settings.soft_limit_usd,
                "max_run_cost_usd": settings.max_run_cost_usd, "estimated_total_usd": total,
                "remaining_usd": max(0, settings.budget_usd - total),
                "soft_limit_reached": total >= settings.soft_limit_usd}

    @router.get("/runs")
    def list_runs():
        with connect() as db:
            return [snapshot(db, row) for row in db.execute("SELECT * FROM qa_runs ORDER BY rowid DESC LIMIT 100")]

    @router.get("/runs/{run_id}")
    def detail(run_id: UUID):
        with connect() as db:
            db.execute("BEGIN")
            return snapshot(db, get(db, str(run_id)), include_timeline=True)

    @router.post("/runs", status_code=202)
    def start(request: RunRequest, key: Key):
        run_id = str(key)
        encoded = request.model_dump_json()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM qa_runs WHERE id = ?", (run_id,)).fetchone()
            if existing:
                if existing["request"] != encoded:
                    raise HTTPException(409, "Use a new idempotency key for an edited run.")
                return snapshot(db, existing)
            if request.mode == "practice":
                if not request.confirm_practice or not request.session_id:
                    raise HTTPException(422, "Select a practice session and confirm the run.")
                if not settings.available or not practice.available:
                    raise HTTPException(503, config_error or settings.configuration_error or "Configure TEAM_KEY on the backend.")
                if db.execute("SELECT 1 FROM qa_runs WHERE session_id=? AND status='running'",
                              (request.session_id,)).fetchone():
                    raise HTTPException(409, "A QA run is already active for this session.")
            elif request.session_id or request.target != "qa_persona":
                raise HTTPException(422, "Mock runs use the synthetic QA persona and their own session.")
            model = "mock-qa" if request.mode == "mock" else settings.model
            db.execute("""INSERT INTO qa_runs (id, request, mode, target, session_id, max_touches, model)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, encoded, request.mode, request.target, request.session_id, request.max_touches, model))
            log_state(db, run_id, "START", {"mode": request.mode})
            data = snapshot(db, get(db, run_id))
        executor.submit(execute_run, run_id, request)
        return data

    @router.post("/runs/{run_id}/stop")
    def stop(run_id: UUID, _key: Key):
        run_id = str(run_id)
        with connect() as db:
            db.execute("UPDATE qa_runs SET stop_requested=1 WHERE id=? AND status='running'", (run_id,))
            return snapshot(db, get(db, run_id))

    return router, lifespan
