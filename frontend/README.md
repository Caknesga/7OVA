# Workflow console

The workflow timeline is the main view. It follows the latest recorded live run by default, or you can select an earlier run from **Run history**. Open **Session activity** to review saved messages, events, and live transcripts.

Run these commands from the repository root in PowerShell.

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run dev
```

Open `http://127.0.0.1:5173/`.

## Connect a local backend

The dashboard reads from your local backend on startup. Vite forwards `/api` reads to `http://127.0.0.1:8000` by default. Set `WORKFLOW_BACKEND_URL` in the shell before starting Vite if your backend uses a different address. The backend supplies the run and timeline data and keeps the Target API team key. The dashboard filters out runs reported with `mock` or `mock_openai` mode.

| Read endpoint | Response |
| --- | --- |
| `GET /api/qa/runs` | Array of run summaries with `id`, `mode`, `status`, `target`, `max_touches`, and `touches_used`. |
| `GET /api/qa/runs/{id}` | Run summary with a `timeline` array of `{state, detail, created_at}` entries. The UI updates an in-progress run every second. |
| `GET /api/scenarios` and `GET /api/sessions` | Optional lists for the target and session sidebar. |
| `GET /api/sessions/{id}/history` | Optional `{messages, events}` for the Conversation and Events views. |
| `GET /api/sessions/{id}/conversations` | Optional array of saved live transcripts. |

The timeline recognizes `START`, `RESEARCH`, `PLAN`, `GENERATE`, `COMMUNICATE`, `EVALUATE`, `MEMORY`, `CONTINUE`, and `STOP`. Run history refreshes every four seconds, and **Refresh** requests new data immediately. The console is read-only.

## Verify

```powershell
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
```
