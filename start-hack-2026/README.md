# START Hack 2026 · revel8 Practice Console

A local workspace for the revel8 practice scenario. Reopen sessions, send manual messages, have text-based live conversations, and run automated QA checks.

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

The **Events** view summarizes observed replies, link opens, form submissions, and attachment opens from the recorded event log. The counts describe simulator activity and do not assign an outcome or trigger any action.

### Live Conversations

1. Select a practice session and open **Live Conversations**. Choose a target and one of Phone call, Video call, Teams call, or Live chat.
2. Click **Start Conversation** to see the simulator's greeting. A start costs one session touch. Each conversation allows up to ten text turns across all four channels.
3. Type one turn at a time and review the reply and conversation events. Use **End Conversation** to close it. The simulator can also end it, and starting another conversation with the same target closes the earlier one in the session.
4. Use **Saved conversation** to reopen a transcript from the selected session. The status and turn counter show when further turns are unavailable.

### AI Agent

Open **AI Agent**, leave **Mock** selected, choose a touch limit of at least two, and click **Start run**. The offline demo uses a fictional test persona and a deterministic LLM fixture. You can watch it start a live chat, send a test turn, end the chat, send a separate message, and record the observed replies. It runs without keys or external API calls.

To run against the official practice simulator, add the following settings to the existing `.env` and restart FastAPI:

- `OPENAI_API_KEY` is your OpenAI project API key. Store it alongside your existing `TEAM_KEY` in the backend environment.
- `OPENAI_MODEL` is a model your OpenAI project can use with the Responses API and strict structured outputs. [Structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs)
- `OPENAI_INPUT_USD_PER_MTOK` and `OPENAI_OUTPUT_USD_PER_MTOK` are the standard prices in USD per million tokens for that model. Set `OPENAI_CACHED_USD_PER_MTOK` if applicable. Omitted cached pricing is estimated at the uncached input rate. [OpenAI pricing](https://openai.com/api/pricing/)
- `OPENAI_BUDGET_USD`, `OPENAI_SOFT_LIMIT_USD`, and `OPENAI_MAX_RUN_COST_USD` default to $20, $15, and $2 respectively.

Create a practice session with **New session**, then choose **Official practice**, the target, and that session in **AI Agent**. Set a touch cap, check the confirmation, and start the run. The dashboard displays the configured model, run history, observations, tokens, and estimated spending. These are local estimates for this database. Check your OpenAI project for billing, credits, and usage outside this dashboard.

The planner may select supported channels and test actions. The generator selects from the approved transparent QA phrases in `backend/qa.py`. OpenAI receives bounded state such as allowed actions and reply indicators. Real QA timelines record outgoing phrases and response metadata. The existing Conversation and Live Conversations views display full simulator transcripts. SQLite adds QA tables on startup.

Run one FastAPI worker. The worker serializes automated runs and reserves estimated cost before each paid request. Unknown charges after a timeout keep their local reserve. **Stop run** prevents further work at the next step boundary, and any in-flight request can finish. If a run stops with a conversation open, you can end it in the manual tab. Interrupted runs remain available in history.

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

## Assess recorded sessions offline

Run the independent evaluator from `start-hack-2026/` with the Python environment
used for the dashboard. It reads an existing SQLite database in read-only mode
and writes two local reports.

```sh
python -m backend.assessment --database data/history.sqlite3 --run-id YOUR_RUN_ID \
  --json-out assessment.json --markdown-out assessment.md
```

Use `--session-id` instead of `--run-id` to assess local manual history.
Alternatively, supply `--input snapshot.json` with any available `run` (including
its timeline), `session`, `history` (messages and events), and `conversations`
records in their original API shapes. SQLite stores run limits but lacks a
complete simulator session metadata snapshot. To verify simulator limits and
scope, attach an already captured session detail with `--session-metadata`.
Use `--reference-profile` to compare structured profile fields with an
independently verified JSON reference containing `id`, `name`, and `role`.

The JSON report includes status, observed evidence, and inferred conclusions for
each finding. A failed check yields `fail`. Warnings or unavailable evidence
yield `partial` when no check failed. A successful CLI exit means both reports
were written. Automated consumers should check the JSON `outcome`. Latency
measures recorded dispatch-to-evaluation elapsed time, which can include retries
and local processing. Free-form claims remain unverified. The evaluator makes
no simulator or model calls.
