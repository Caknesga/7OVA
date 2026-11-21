# START Hack 2026 · revel8 Practice Console

A local Step 2 workspace for discovering the practice scenario, creating and reopening sessions, choosing a simulator target and message channel, and reviewing replies and events. Every message is composed and sent manually.

## Install

Use Python 3.11 or newer and Node.js 22.12 or newer. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
cp .env.example .env
```

Set `TEAM_KEY` in the root `.env` file or export it in the backend shell. FastAPI reads the key and sends it as a Bearer credential only to the [official revel8 Target API](https://target-api.apps.01.cf.eu01.stackit.cloud/docs.md). The browser talks to the local backend through Vite's `/api` proxy. Keep both servers bound to loopback.

## Run locally

Start FastAPI from the repository root:

```sh
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In a second terminal, also from the repository root:

```sh
npm --prefix frontend run dev
```

Open [the practice dashboard](http://127.0.0.1:5173/). Create a practice session or select an existing one. The Conversation view groups replies with the messages they reference. The Events view lists replies and other observable events. Refresh reloads simulator state. Remaining touches come from the simulator's budget and usage counters.

## History and retries

- SQLite stores messages sent by this dashboard and the events it receives in `data/history.sqlite3`. Sessions created elsewhere may show replies and other events without the original outgoing messages because the event endpoint does not return those message bodies. Use a separate `DATABASE_PATH` for each team if you switch keys.
- Simulator requests have a 120-second HTTP timeout. After a transient send error, wait a few seconds and submit the unchanged draft again from the same page so the dashboard can reuse the idempotency key. An edited message uses a fresh key.
- After an uncertain session creation, check the refreshed session list. The upstream documentation guarantees idempotency for messages but doesn't specify it for session creation, so the backend blocks blind replays of an uncertain creation key.
- The backend creates and operates on practice sessions only. Event URLs and message text are displayed as text. The application makes no outbound requests to those URLs or to external messaging services.

## Verify

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend run build
```

For an offline browser walkthrough, run this command in place of the normal FastAPI command and start Vite as above:

```sh
DATABASE_PATH="$(mktemp -d)/history.sqlite3" \
  .venv/bin/python -m uvicorn tests.mock_simulator:create_mock_app --factory --host 127.0.0.1 --port 8000
```

The mock backend uses fictional targets and canned responses. Creating a session and sending a message exercises the touch counter, linked replies, observable events, and local history without accessing the live simulator.
