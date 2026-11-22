

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .schemas import (
    ResearchExtraction,
    ResearchMetadata,
    ResearchReport,
    SourceReference,
    TargetIdentity,
)

load_dotenv()

FORMAT_MODEL = os.getenv("RESEARCH_FORMAT_MODEL", "gpt-4o-mini")
MAX_REPORT_CHARS = 50_000  # The original, untruncated report is always saved.


SYSTEM_PROMPT = """You extract structured facts from a public professional research report.
Only use the report and the source IDs supplied in the user message.
Extract company-level and person-specific PUBLIC PROFESSIONAL facts separately.
Examples: professional role, publicly announced project, publication, public talk.
Do not extract private addresses, personal contact details, family, private life,
psychological vulnerabilities, susceptibility, or speculative personal attributes.
Do not invent facts, dates, sources, URLs, people, or citations.
For each claim, reference only source IDs that are explicitly associated with it
in the report. If unsure, leave source_ids empty and record the uncertainty.
All verification_status values must be 'unverified' or 'uncertain': neither the
research report nor a matching URL proves the source supports the claim.
For person_findings.category, use a concise category such as 'professional_role',
'professional_project', 'publication', 'public_talk', or 'other'.
Keep claims specific and concise. Put absent important evidence in research_gaps.
Return the structured object requested by the response schema."""


def _normalize_sources(raw_sources: list) -> list[SourceReference]:
    """Use only source URLs actually returned by GPT Researcher."""
    normalized = []
    seen = set()
    for item in raw_sources or []:
        if isinstance(item, str):
            url, title, publisher = item.strip(), "", ""
        elif isinstance(item, dict):
            url = str(item.get("url") or item.get("source_url") or "").strip()
            title = str(item.get("title") or "")
            publisher = str(item.get("publisher") or "")
        else:
            continue

        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        normalized.append(
            SourceReference(
                id=f"source_{len(normalized) + 1}",
                url=url,
                title=title,
                publisher=publisher,
            )
        )
    return normalized


def _sanitize_extraction(extraction: ResearchExtraction, known_sources: set[str]):
    """Discard fabricated source IDs and prevent unsupported 'verified' labels."""
    warnings = []
    for finding in [*extraction.company_findings, *extraction.person_findings]:
        original = list(finding.source_ids)
        finding.source_ids = list(dict.fromkeys(
            sid for sid in original if sid in known_sources
        ))
        invalid = sorted(set(original) - known_sources)
        if invalid:
            warnings.append(
                f"Removed unknown source IDs {invalid} from claim: {finding.claim}"
            )
        # A cited URL alone does not verify that the URL supports this claim.
        finding.verification_status = "unverified" if finding.source_ids else "uncertain"
    extraction.uncertainties.extend(warnings)
    return extraction


async def format_research(
    target: dict,
    research_result: dict,
    scenario_id: str = "practice",
) -> dict:
    """Return the exact JSON-ready dict required by the Planning Agent.

    Args:
        target: Dict from GET /scenarios, including id/name/role/company.
        research_result: Dict from existing research(query) function:
            {"query": str, "report": str, "sources": list[str | dict]}
        scenario_id: Defaults to 'practice'; pass 'eval' when appropriate.
    """
    identity = TargetIdentity(
        id=str(target["id"]),
        name=str(target["name"]),
        role=str(target.get("role") or ""),
        company=str(target.get("company") or ""),
        scenario_id=scenario_id,
    )
    query = str(research_result.get("query") or "")
    report_text = str(research_result.get("report") or "")
    sources = _normalize_sources(research_result.get("sources") or [])

    extraction = ResearchExtraction(
        company_findings=[], person_findings=[],
        research_gaps=[], uncertainties=[],
    )
    status = "completed"
    if not report_text.strip():
        status = "partial"
        extraction.research_gaps.append("GPT Researcher returned an empty report.")
    else:
        source_catalog = [
            {"id": source.id, "url": source.url, "title": source.title}
            for source in sources
        ]
        user_payload = {
            "target": identity.model_dump(),
            "sources": source_catalog,
            "research_report": report_text[:MAX_REPORT_CHARS],
        }
        try:
            client = AsyncOpenAI()  # Reads OPENAI_API_KEY from environment.
            completion = await client.chat.completions.parse(
                model=FORMAT_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                response_format=ResearchExtraction,
            )
            parsed = completion.choices[0].message.parsed
            if parsed is None:
                raise RuntimeError("Model did not return a parsed extraction.")
            extraction = _sanitize_extraction(
                parsed, {source.id for source in sources}
            )
        except Exception as exc:
            # Preserve the original research, even when formatting fails.
            status = "partial"
            extraction.research_gaps.append("Structured extraction did not complete.")
            extraction.uncertainties.append(
                f"Formatter error ({type(exc).__name__}); see logs for details."
            )
            print(f"[Research Formatter] Extraction failed: {exc}")

    if len(report_text) > MAX_REPORT_CHARS:
        status = "partial"
        extraction.research_gaps.append(
            "Structured extraction used only the first "
            f"{MAX_REPORT_CHARS} characters; the full original report is included."
        )
    if not sources:
        status = "partial"
        extraction.research_gaps.append("GPT Researcher returned no usable source URLs.")

    extraction.uncertainties.append(
        "Source URLs were not independently opened or checked against the claims; "
        "cited claims remain unverified."
    )

    final = ResearchReport(
        target=identity,
        research_metadata=ResearchMetadata(
            query=query,
            researched_at=datetime.now(timezone.utc).isoformat(),
            status=status,
        ),
        company_findings=extraction.company_findings,
        person_findings=extraction.person_findings,
        sources=sources,
        research_gaps=extraction.research_gaps,
        uncertainties=extraction.uncertainties,
        full_report_markdown=report_text,
    )
    return final.model_dump(mode="json")

