"""SQLite storage for practice conversations and their transcripts."""

import json
from typing import Literal, get_args

from fastapi import HTTPException

LiveChannel = Literal["phone_call", "video_call", "teams_call", "live_chat"]
LIVE_CHANNELS = get_args(LiveChannel)
MAX_TURNS = 10

SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        session TEXT NOT NULL,
        target TEXT NOT NULL,
        channel TEXT NOT NULL,
        greeting TEXT,
        status TEXT NOT NULL,
        ended INTEGER NOT NULL,
        turns_used INTEGER NOT NULL CHECK(turns_used BETWEEN 0 AND {MAX_TURNS}),
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX IF NOT EXISTS conversations_by_session ON conversations(session);
    CREATE TABLE IF NOT EXISTS conversation_turns (
        conversation TEXT NOT NULL REFERENCES conversations(id),
        number INTEGER NOT NULL CHECK(number BETWEEN 1 AND {MAX_TURNS}),
        say TEXT NOT NULL,
        persona_says TEXT,
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(conversation, number)
    );
"""


def get(db, cid):
    row = db.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise HTTPException(404, "Conversation not found in local practice history.")
    return row


def list_for_session(db, sid):
    conversations = {}
    for row in db.execute("""
        SELECT id AS conversation_id, target AS "to", channel, greeting,
               status, ended, turns_used, started_at
        FROM conversations WHERE session = ? ORDER BY rowid
    """, (sid,)):
        entry = dict(row)
        entry.update(ended=bool(row["ended"]), max_turns=MAX_TURNS, turns=[], events=[])
        conversations[row["conversation_id"]] = entry

    if not conversations:
        return []

    for row in db.execute("""
        SELECT t.conversation, t.number, t.say, t.persona_says, t.recorded_at
        FROM conversation_turns AS t JOIN conversations AS c ON c.id = t.conversation
        WHERE c.session = ? ORDER BY t.conversation, t.number
    """, (sid,)):
        turn = dict(row)
        conversations[turn.pop("conversation")]["turns"].append(turn)

    for row in db.execute("""
        SELECT payload FROM history
        WHERE session = ? AND kind = 'event'
          AND json_type(payload, '$.conversation_id') = 'text'
        ORDER BY rowid
    """, (sid,)):
        event = json.loads(row["payload"])
        conversation = conversations.get(event["conversation_id"])
        if conversation is not None:
            conversation["events"].append(event)

    return list(conversations.values())


def record_start(db, sid, target, channel, receipt):
    cid = receipt.conversation_id
    existing = db.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
    if existing:
        if (existing["session"], existing["target"], existing["channel"]) != (sid, target, channel):
            raise HTTPException(502, "Simulator returned a conversation ID already in use.")
        return

    # The simulator closes the previous call with this persona in the session.
    db.execute("""
        UPDATE conversations SET ended = 1, status = 'ended'
        WHERE session = ? AND target = ? AND ended = 0
    """, (sid, target))
    db.execute("""
        INSERT INTO conversations (id, session, target, channel, greeting, status, ended, turns_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (cid, sid, target, channel, receipt.persona_says,
          receipt.status, receipt.ended, receipt.turns_used))


def record_turn(db, cid, say, receipt):
    number = receipt.turns_used
    saved = db.execute("""
        INSERT INTO conversation_turns (conversation, number, say, persona_says)
        VALUES (?, ?, ?, ?) ON CONFLICT(conversation, number) DO NOTHING
    """, (cid, number, say, receipt.persona_says))
    if saved.rowcount == 0:
        turn = db.execute("""
            SELECT say, persona_says FROM conversation_turns
            WHERE conversation = ? AND number = ?
        """, (cid, number)).fetchone()
        if tuple(turn) != (say, receipt.persona_says):
            raise HTTPException(502, "Simulator returned a conflicting turn number.")

    db.execute("""
        UPDATE conversations SET turns_used = MAX(turns_used, ?),
            status = CASE WHEN ended = 0 THEN ? ELSE status END,
            ended = MAX(ended, ?)
        WHERE id = ?
    """, (number, receipt.status, receipt.ended, cid))


def close(db, cid):
    db.execute("UPDATE conversations SET ended = 1, status = 'ended' WHERE id = ?", (cid,))
