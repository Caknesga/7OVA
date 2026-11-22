
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from gpt_researcher import GPTResearcher

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


async def research(query: str) -> dict:
    agent = GPTResearcher(
        query=query,
        report_type="research_report",
    )

    # Search the internet and collect information
    await agent.conduct_research()

    # Generate a report
    report = await agent.write_report()

    # Collect source URLs
    sources = agent.get_source_urls()

    return {
        "query": query,
        "report": report,
        "sources": sources,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: python research/agent.py "Your research query"'
        )

    query = " ".join(sys.argv[1:])
    result = asyncio.run(research(query))

    output_dir = ROOT / "research" / "results"
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "report.md").write_text(
        result["report"], encoding="utf-8"
    )

    (output_dir / "research.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(result["report"])
    print(f"\nSources found: {len(result['sources'])}")
    print(f"Saved results to: {output_dir}")
