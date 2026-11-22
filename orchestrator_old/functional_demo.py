"""Run the OpenAI planner through the OFFLINE orchestrator without pytest.

Usage (from repository root):
    python -m orchestrator.functional_demo

This invokes the real OpenAI PlanningAgent but uses the Orchestrator's
OfflineCommunicationPort. It does not send messages through the Target API.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from uuid import uuid4

from planning.agent import PlanningAgent
from orchestrator_old.controller import Orchestrator, OfflineCommunicationPort
from orchestrator_old.state import RunState


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    template_path = root / "planning" / "example_input.json"
    inputs = json.loads(template_path.read_text(encoding="utf-8"))

    # Each functional test gets a unique ID, keeping previous log files separate.
    session_id = f"qa_{uuid4().hex[:8]}"
    inputs["simulator_environment"]["session_id"] = session_id

    target_id = inputs["simulator_environment"]["target_id"]
    research_target_id = inputs["research_report"]["target"]["id"]
    if target_id != research_target_id:
        raise ValueError("Target IDs disagree between simulator and research input")

    if "planner_inputs" not in inspect.signature(Orchestrator).parameters:
        raise RuntimeError(
            "Your Orchestrator constructor still lacks 'planner_inputs'. "
            "Apply the planner-integration edits before running this demo."
        )

    state = RunState(
        session_id=session_id,
        scenario_id="offline_qa",
        target_id=target_id,
        touch_budget=inputs["simulator_environment"]["remaining_touch_budget"],
    )

    orchestrator = Orchestrator(
        planner=PlanningAgent(),
        planner_inputs=inputs,
        communication=OfflineCommunicationPort(),
        max_steps=3,  # Bound OpenAI calls and prevent unending test actions.
        log_dir=root / "logs",
        state_dir=root / "orchestrator" / "state",
        evaluation_dir=root / "evaluation" / "results",
    )

    print("Functional test: OpenAI planner + OFFLINE communication + evaluation")
    print("Session:", session_id)
    result = await orchestrator.run(state)

    print("\nFINAL STATE")
    print(result.model_dump_json(indent=2))

    log_path = root / "logs" / f"{session_id}.jsonl"
    if log_path.exists():
        print("\nINTERACTION LOG")
        for number, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), 1):
            entry = json.loads(line)
            action = entry.get("planned_action", {})
            response = entry.get("communication_result", {})
            print(f"Step {number}: {action.get('action_type')}, "
                  f"success={response.get('success')}, "
                  f"persona_says={response.get('persona_says')!r}, "
                  f"events={response.get('events', [])}")
        print("Log file:", log_path)
    else:
        print("\nNo interactions logged; inspect final_state.last_error")

    report_path = root / "evaluation" / "results" / f"{session_id}_evaluation.md"
    if report_path.exists():
        print("Evaluation report:", report_path)
    print("Checkpoint:", root / "orchestrator" / "state" / f"{session_id}.json")


if __name__ == "__main__":
    asyncio.run(main())
