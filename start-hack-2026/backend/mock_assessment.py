"""Run the offline QA demo and assess its saved records."""

import tempfile
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from .assessment import main as write_reports
from .main import create_app


def reject_network(request):
    raise AssertionError("The offline demo attempted an upstream request.")


def main():
    directory = Path(tempfile.mkdtemp(prefix="start-hack-mock-"))
    database = directory / "history.sqlite3"
    with TestClient(create_app("", database, httpx.MockTransport(reject_network))) as client:
        response = client.post("/api/qa/runs", headers={"Idempotency-Key": str(uuid4())},
                               json={"mode": "mock", "target": "qa_persona", "max_touches": 2})
        response.raise_for_status()
        run = response.json()
        deadline = monotonic() + 10
        while run["status"] == "running" and monotonic() < deadline:
            sleep(0.05)
            response = client.get(f"/api/qa/runs/{run['id']}")
            response.raise_for_status()
            run = response.json()
    machine, human = directory / "assessment.json", directory / "assessment.md"
    write_reports(["--database", str(database), "--run-id", run["id"],
                   "--json-out", str(machine), "--markdown-out", str(human)])
    print(f"Mock run {run['status']} ({run['touches_used']}/2 touches)")
    print(f"Markdown report  {human}")
    print(f"JSON report      {machine}")
    return 0 if run["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
