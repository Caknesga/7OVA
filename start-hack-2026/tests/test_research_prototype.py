"""Runnable source-checking exercise for a fixed, fictional QA profile."""

import json


FIXTURE_PROFILE = {
    "name": "Practice QA Assistant",
    "role": "Fictional simulator test persona",
    "project": "Example QA Dashboard",
    "release_date": "2026-01-15",
}

FIXTURE_SOURCES = {
    "Fictional demo program": {
        "name": "Practice QA Assistant",
        "role": "Fictional simulator test persona",
        "project": "Example QA Dashboard",
    },
    "Fictional project notes": {
        "role": "Demo coordinator",
        "project": "Example QA Dashboard",
    },
}


def audit_fixture_profile():
    """Compare each fixture claim with the sources that mention it."""
    report = {}
    for field, claimed_value in FIXTURE_PROFILE.items():
        evidence = {
            source: facts[field]
            for source, facts in FIXTURE_SOURCES.items()
            if field in facts
        }
        if not evidence:
            status = "missing"
        elif any(value != claimed_value for value in evidence.values()):
            status = "conflicting"
        else:
            status = "supported"
        report[field] = {"claimed_value": claimed_value, "status": status, "evidence": evidence}
    return report


def test_agreeing_sources_support_the_project_claim():
    assert audit_fixture_profile()["project"] == {
        "claimed_value": "Example QA Dashboard",
        "status": "supported",
        "evidence": {
            "Fictional demo program": "Example QA Dashboard",
            "Fictional project notes": "Example QA Dashboard",
        },
    }


def test_disagreement_keeps_both_source_values():
    role = audit_fixture_profile()["role"]
    assert role["status"] == "conflicting"
    assert role["evidence"] == {
        "Fictional demo program": "Fictional simulator test persona",
        "Fictional project notes": "Demo coordinator",
    }


def test_absent_evidence_leaves_the_claim_unverified():
    date = audit_fixture_profile()["release_date"]
    assert date["status"] == "missing"
    assert date["evidence"] == {}


if __name__ == "__main__":
    print(json.dumps(audit_fixture_profile(), indent=2))
