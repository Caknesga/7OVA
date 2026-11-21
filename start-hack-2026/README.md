# START Hack 2026 · revel8 Practice Console

A local workspace for the revel8 practice scenario. Reopen sessions, send messages, and have text-based live conversations with simulator personas. Every message and conversation turn is sent manually.

## Install

Use Python 3.11 or newer and Node.js 22.12+ LTS or 24+. Run the commands from the `start-hack-2026/` project directory in the shared repository.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
test -e .env || cp .env.example .env
```

Set `TEAM_KEY` in the root `.env` file or export it in the backend shell. FastAPI reads the key and sends it as a Bearer credential only to the [official revel8 Target API](https://target-api.apps.01.cf.eu01.stackit.cloud/docs.md). The browser talks to the local backend through Vite's `/api` proxy. Keep both servers bound to loopback.

## Run locally

Start FastAPI from `start-hack-2026/`:

```sh
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In a second terminal, also from `start-hack-2026/`:

```sh
npm --prefix frontend run dev
```

Open [the practice dashboard](http://127.0.0.1:5173/). Create a practice session or select an existing one. The Conversation view groups replies with the messages they reference. The Events view lists replies and other observable events. Refresh reloads simulator state. Remaining touches come from the simulator's budget and usage counters.

### Live Conversations

1. Select a practice session and open **Live Conversations**. Choose a target and one of Phone call, Video call, Teams call, or Live chat.
2. Click **Start Conversation** to see the simulator's greeting. A start costs one session touch. Each conversation allows up to ten text turns across all four channels.
3. Type one turn at a time and review the reply and conversation events. Use **End Conversation** to close it. The simulator can also end it, and starting another conversation with the same target closes the earlier one in the session.
4. Use **Saved conversation** to reopen a transcript from the selected session. The status and turn counter show when further turns are unavailable.

## History and retries

- SQLite stores outgoing messages, events, live greetings, and ordered conversation turns in `data/history.sqlite3`. Starting the updated backend adds the live conversation tables to an existing database. Simulator sessions created elsewhere may show events without their original messages or transcripts. Use a separate `DATABASE_PATH` for each team if you switch keys.
- Simulator requests have a 120-second HTTP timeout. After a transient send error, wait a few seconds and submit the unchanged draft again from the same page so the dashboard can reuse the idempotency key. An edited message uses a fresh key.
- After a transient live error, wait a few seconds and use **Retry Start** or **Retry Turn**. The browser tab keeps the original request and idempotency key through a refresh. A successful retry recovers the original start or turn without consuming another touch or turn.
- After an uncertain session creation, check the refreshed session list. The upstream documentation guarantees idempotency for messages but doesn't specify it for session creation, so the backend blocks blind replays of an uncertain creation key.
- The backend creates and operates on practice sessions only. Event URLs and message text are displayed as text. The application makes no outbound requests to those URLs or to external messaging services.

## Verify

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

For an offline browser walkthrough, run this command in place of the normal FastAPI command and start Vite as above:

```sh
DATABASE_PATH="$(mktemp -d)/history.sqlite3" \
  .venv/bin/python -m uvicorn tests.mock_simulator:create_mock_app --factory --host 127.0.0.1 --port 8000
```

The mock backend uses fictional targets and canned responses. In **Live Conversations**, start a conversation, send a practice turn, and check the reply and event list. Send `Goodbye.` to exercise the simulator ending a conversation, or use **End Conversation** to end it yourself. This mode requires no team key and makes no external simulator requests.
