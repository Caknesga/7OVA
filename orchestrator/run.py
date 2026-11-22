"""Command-line entry point for offline QA or opt-in live practice QA.

    python -m orchestrator.run --mode offline --planner scripted
    python -m orchestrator.run --mode offline --planner openai
    python -m orchestrator.run --mode practice --live --target robert --planner scripted
    python -m orchestrator.run --mode practice --live --target robert --planner openai

The practice mode sends only fixed, transparent QA text to the simulated Target
API. It never sends email/SMS to real people and cannot access eval scoring.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from planning.agent import PlanningAgent

from .controller import Orchestrator, ScriptedTestPlanner
from .live import QA_EMAIL, QA_SUBJECT, PracticeQAOrchestrator
from .state import RunState


def _inputs(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _research_metadata(target: dict, company: str) -> dict:
    """Honest metadata-only fallback when an external report wasn't supplied."""
    return {
        "schema_version": "1.0",
        "target": {
            "id": target["id"], "name": target["name"],
            "role": target.get("role", ""), "company": company,
            "scenario_id": "practice",
        },
        "research_metadata": {"query": "", "researched_at": "", "status": "not_run"},
        "company_findings": [], "person_findings": [], "sources": [],
        "research_gaps": ["No external research report supplied"],
        "uncertainties": [], "full_report_markdown": "",
    }


async def _run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    data = _inputs(args.inputs or root / "planning" / "example_input.json")

    if args.mode == "offline":
        session = f"qa_{uuid4().hex[:10]}"
        data["simulator_environment"]["session_id"] = session
        data["simulator_environment"]["target_id"] = data["research_report"]["target"]["id"]
        planner = (PlanningAgent() if args.planner == "openai" else
                   ScriptedTestPlanner([{
                       "action_type": "send_message", "target_id": data["research_report"]["target"]["id"],
                       "channel": "email", "sender": {"name": "QA Tester"},
                       "subject": QA_SUBJECT, "body": QA_EMAIL,
                   }]))
        orchestrator = Orchestrator(
            planner, planner_inputs=data, max_steps=args.max_steps,
            log_dir=root / "logs", state_dir=root / "orchestrator" / "state",
            evaluation_dir=root / "evaluation" / "results",
        )
        state = RunState(
            session_id=session,
            target_id=data["research_report"]["target"]["id"],
            touch_budget=data["simulator_environment"]["remaining_touch_budget"],
        )
        print("OFFLINE QA — no Target API contacts")
        finished = await orchestrator.run(state)
    else:
        if not args.live:
            raise ValueError("Practice mode requires explicit --live opt-in")
        if not args.target:
            raise ValueError("Practice mode requires --target TARGET_ID")
        from communicate.client import TargetAPIClient

        async with TargetAPIClient() as client:
            scenarios = await client.get_scenarios()
            practice = next((s for s in scenarios if s.get("id") == "practice"), None)
            if not practice or not practice.get("open"):
                raise ValueError("Practice scenario is not currently open")
            target = next((t for t in practice.get("targets", [])
                           if t.get("id") == args.target), None)
            if target is None:
                raise ValueError(f"Unknown practice target ID: {args.target}")

            if args.research_file:
                report = _inputs(args.research_file)
                if report.get("target", {}).get("id") != args.target:
                    raise ValueError("Selected research file belongs to another target")
            else:
                report = _research_metadata(target, practice.get("company", ""))
            data["research_report"] = report
            data["simulator_environment"]["target_id"] = args.target
            data["simulator_environment"]["session_id"] = args.session or "pending"
            data["simulator_environment"]["allowed_channels"] = ["email", "live_chat"]
            data["operational_constraints"]["permitted_sender"] = {"name": "QA Tester"}

            planner = (PlanningAgent() if args.planner == "openai" else
                       ScriptedTestPlanner([{
                           "action_type": "send_message", "target_id": args.target,
                           "channel": "email", "sender": {"name": "QA Tester"},
                           "subject": QA_SUBJECT, "body": QA_EMAIL,
                       }]))
            runner = PracticeQAOrchestrator(
                client=client, planner=planner, planner_inputs=data,
                max_steps=args.max_steps,
                log_dir=root / "logs", state_dir=root / "orchestrator" / "state",
                evaluation_dir=root / "evaluation" / "results",
            )
            state = await runner.prepare(args.target, existing_session_id=args.session)
            print(f"LIVE SIMULATOR PRACTICE QA — target={args.target}, session={state.session_id}")
            finished = await runner.run(state)

    print("\nFINAL RUN STATE")
    print(finished.model_dump_json(indent=2))
    print(f"\nLog: {root / 'logs' / (finished.session_id + '.jsonl')}")
    print(f"State: {root / 'orchestrator' / 'state' / (finished.session_id + '.json')}")
    print(f"Report: {root / 'evaluation' / 'results' / (finished.session_id + '_evaluation.md')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic simulator QA orchestrator")
    parser.add_argument("--mode", choices=["offline", "practice"], default="offline")
    parser.add_argument("--planner", choices=["scripted", "openai"], default="scripted")
    parser.add_argument("--target", help="Practice target ID from GET /scenarios")
    parser.add_argument("--live", action="store_true", help="Explicitly allow simulated practice contact")
    parser.add_argument("--session", help="Resume a local checkpoint for an existing practice session")
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--inputs", type=Path, help="Planner input JSON template")
    parser.add_argument("--research-file", type=Path, help="Validated research JSON for chosen target")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
