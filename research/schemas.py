"""Versioned JSON contract for the Research Agent -> Planning Agent boundary."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


VerificationStatus = Literal["verified", "unverified", "uncertain"]
ResearchStatus = Literal["completed", "partial", "failed"]


class TargetIdentity(StrictModel):
    id: str
    name: str
    role: str
    company: str
    scenario_id: str


class ResearchMetadata(StrictModel):
    query: str
    researched_at: str  # ISO 8601 UTC timestamp
    status: ResearchStatus


class SourceReference(StrictModel):
    id: str
    url: str
    title: str
    publisher: str


class CompanyFinding(StrictModel):
    claim: str
    source_ids: list[str]
    verification_status: VerificationStatus


class PersonFinding(StrictModel):
    claim: str
    category: str
    source_ids: list[str]
    verification_status: VerificationStatus


class ResearchExtraction(StrictModel):
    """LLM-generated subset; identity, URLs, and full report are set by code."""

    company_findings: list[CompanyFinding]
    person_findings: list[PersonFinding]
    research_gaps: list[str]
    uncertainties: list[str]


class ResearchReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    target: TargetIdentity
    research_metadata: ResearchMetadata
    company_findings: list[CompanyFinding] = Field(default_factory=list)
    person_findings: list[PersonFinding] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    research_gaps: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    full_report_markdown: str = ""

    @model_validator(mode="after")
    def ensure_sources_exist(self):
        known_ids = {source.id for source in self.sources}
        for finding in [*self.company_findings, *self.person_findings]:
            unknown = set(finding.source_ids) - known_ids
            if unknown:
                raise ValueError(f"Finding references unknown source IDs: {sorted(unknown)}")
            if len(finding.source_ids) != len(set(finding.source_ids)):
                raise ValueError("Duplicate source IDs in finding")
        return self
