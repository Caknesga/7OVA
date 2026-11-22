
import os
import re
import json
import asyncio
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv

from research.agent import research
from research.formatter import format_research

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API = "https://target-api.apps.01.cf.eu01.stackit.cloud/v1"
load_dotenv(ROOT / ".env", override=True)

TEAM_KEY = os.getenv("TEAM_KEY", "").strip()
TEAM_KEY = TEAM_KEY.removeprefix("Bearer ").strip()

if not TEAM_KEY:
    raise RuntimeError("TEAM_KEY is missing")

RESULT_DIR = ROOT / "research" / "results"
RESULT_DIR.mkdir(parents=True, exist_ok=True)


def get_targets(scenario_id="practice"):
    """Fetch target metadata from the hackathon API."""

    if not TEAM_KEY:
        raise RuntimeError("TEAM_KEY missing from .env")

    response = requests.get(
        f"{API}/scenarios",
        headers={
            "Authorization": f"Bearer {TEAM_KEY}"
        },
        timeout=30,
    )
    response.raise_for_status()

    scenarios = response.json()

    for scenario in scenarios:
        if scenario["id"] == scenario_id:
            return [
                {
                    **target,
                    "company": scenario["company"],
                }
                for target in scenario.get("targets", [])
            ]

    raise ValueError(f"Scenario not found: {scenario_id}")


def build_query(target):
    """Build a public professional research query."""

    return f"""
    Research the publicly documented professional
    activities of the following person.

    Name: {target['name']}
    Role: {target.get('role', 'Unknown')}
    Company: {target['company']}

    Focus on:
    - Professional background
    - Publicly announced projects
    - Conference presentations and interviews
    - Published technical work
    - Recent company announcements

    Provide source URLs and dates.
    Distinguish verified facts from uncertain claims.
    Exclude private contact details, personal
    vulnerabilities, and speculative information.
    """


async def research_target(target):
    """Run GPT Researcher for a target."""

    target_id = target["id"]

    print(f"\nResearching: {target['name']}")

    query = build_query(target)
    result = await research(query)

    output = await format_research(
        target=target,
        research_result=result,
        scenario_id="practice",
    )

    filename = re.sub(
        r"[^a-zA-Z0-9_-]", "_", target_id
    ) + ".json"

    filepath = RESULT_DIR / filename

    filepath.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Saved: {filepath}")
    return output


async def main(target_id=None):
    targets = get_targets()

    if target_id:
        targets = [
            t for t in targets if t["id"] == target_id
        ]

        if not targets:
            raise ValueError(f"Unknown target: {target_id}")

    # Sequential execution avoids excessive API usage.
    for target in targets:
        await research_target(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=None)
    args = parser.parse_args()

    asyncio.run(main(args.target))
