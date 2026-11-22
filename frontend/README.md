# 7_Social_Hack

The workflow timeline is the main view. Use **Target** to filter saved practice runs, then follow that target's latest run or select an earlier one from **Run history**. Select **Replay timeline** to watch a saved run move through its recorded steps. Open **Session activity** to review saved messages, events, and conversation metadata.

Start the records API from the repository root in PowerShell. It reads practice checkpoints in `orchestrator/state/`, communication logs in `logs/`, and evaluation reports in `evaluation/results/`.

```powershell
& '.\.venv\Scripts\python.exe' -m uvicorn workflow_api:app --host 127.0.0.1 --port 8000
```

For a fresh Python environment, install the FastAPI and Uvicorn dependencies from `start-hack-2026/requirements.txt` first. The records server reads local files without using a Target API team key.

In a second PowerShell terminal at the repository root, start the frontend.

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run dev
```

Open `http://127.0.0.1:5173/`. Choose a recorded run under **Run history**, then select a stage to inspect its details.

## Connect a local backend

The dashboard reads from your local backend on startup. Vite forwards `/api` reads to `http://127.0.0.1:8000` by default. Set `WORKFLOW_BACKEND_URL` in the shell before starting Vite if your backend uses a different address. The records API lists checkpoints marked `practice`. It displays recorded stages and event metadata, while optional transcript text depends on the connected backend.

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
