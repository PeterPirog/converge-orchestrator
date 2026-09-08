"""Deterministic reviewer blocking scope for incremental convergence.

Immutable requirements remain authoritative for the whole run, but an intermediate candidate may
only be blocked on: its current target requirements, already-satisfied requirements (the
non-regression contract), defects it actually introduces, and its Task Envelope. Reviewer prompts
label these scopes deterministically (see prompts.review_scope_sections) and :func:`scope_review`
demotes blockers whose only requirement citation is a future/pending non-target requirement, so a
reviewer can never convert a future functional requirement into a present blocker. Blockers that
cite an in-scope requirement, cite no requirement, or cite an unknown requirement id fail closed
and stay blocking. The deterministic risk classifier is unaffected.
"""

from __future__ import annotations

from typing import Any

from .models import (
    ComplianceSnapshot,
    Requirement,
    RequirementStatus,
    ReviewFinding,
    ReviewResult,
)

SATISFIED_STATUSES = frozenset({RequirementStatus.PASS, RequirementStatus.PARTIAL})

SCOPING_POLICY = (
    "blockers whose only requirement citation is a future/pending non-target requirement are "
    "outside the candidate's blocking scope and are demoted to notes"
)


def requirement_statuses(
    requirements: list[Requirement],
    compliance: ComplianceSnapshot | None = None,
) -> dict[str, RequirementStatus]:
    """Return the per-requirement status used for scoping decisions.

    The run-state compliance snapshot is authoritative when it contains an entry; the static
    contract status is the fallback.
    """

    statuses = {requirement.id: requirement.status for requirement in requirements}
    if compliance is not None:
        for requirement_id, entry in compliance.entries.items():
            if requirement_id in statuses:
                statuses[requirement_id] = entry.status
    return statuses


def blocking_requirement_ids(
    *,
    target_requirement_ids: set[str] | list[str],
    requirements: list[Requirement],
    compliance: ComplianceSnapshot | None = None,
) -> set[str]:
    """Return the requirement IDs a reviewer may block on for the current candidate.

    Blocking scope = current target requirements + already-satisfied requirements (pass or
    partially satisfied by already-integrated work). Future/pending non-target requirements stay
    authoritative roadmap goals but must not block the current candidate.
    """

    statuses = requirement_statuses(requirements, compliance)
    blocking = set(target_requirement_ids)
    blocking.update(
        requirement_id
        for requirement_id, status in statuses.items()
        if status in SATISFIED_STATUSES
    )
    return blocking


def scope_review(
    *,
    review: ReviewResult,
    target_requirement_ids: set[str] | list[str],
    requirements: list[Requirement],
    compliance: ComplianceSnapshot | None = None,
) -> tuple[ReviewResult, dict[str, Any]]:
    """Enforce deterministic blocking scope on a parsed review result.

    A blocker whose ``requirement_id`` is a known future/pending non-target requirement is demoted
    to a note. When a rejecting lane's blockers were all demoted this way, that lane no longer
    drives the aggregate verdict. Conservative hard rejects (no blocker findings, majors-only
    rejects) and blockers without an in-scope citation stay untouched. The returned scoping report
    is empty when nothing was demoted and no lane verdict changed.
    """

    known_ids = {requirement.id for requirement in requirements}
    blocking = blocking_requirement_ids(
        target_requirement_ids=target_requirement_ids,
        requirements=requirements,
        compliance=compliance,
    )

    demotions: list[dict[str, str]] = []
    findings: list[ReviewFinding] = []
    for finding in review.findings:
        if (
            finding.severity == "blocker"
            and finding.requirement_id
            and finding.requirement_id in known_ids
            and finding.requirement_id not in blocking
        ):
            demotions.append(
                {
                    "reviewer": finding.reviewer or "",
                    "requirement_id": finding.requirement_id,
                    "original_severity": "blocker",
                    "scoped_severity": "note",
                    "reason": finding.reason,
                }
            )
            findings.append(finding.model_copy(update={"severity": "note"}))
        else:
            findings.append(finding)

    scoped_verdicts: dict[str, str] = {}
    for role, verdict in review.reviewers.items():
        if verdict != "reject":
            scoped_verdicts[role] = verdict
            continue
        role_blockers = [
            finding
            for finding in findings
            if finding.severity == "blocker" and finding.reviewer == role
        ]
        role_demotions = [item for item in demotions if item["reviewer"] == role]
        if role_blockers or not role_demotions:
            scoped_verdicts[role] = "reject"
        else:
            scoped_verdicts[role] = "pass"

    if scoped_verdicts:
        verdict = "reject" if "reject" in scoped_verdicts.values() else "pass"
    else:
        remaining_blockers = [finding for finding in findings if finding.severity == "blocker"]
        if review.verdict == "reject" and not remaining_blockers and demotions:
            verdict = "pass"
        else:
            verdict = review.verdict

    unchanged = not demotions and verdict == review.verdict and scoped_verdicts == dict(
        review.reviewers
    )
    if unchanged:
        return review, {}

    scoped = review.model_copy(
        update={
            "verdict": verdict,
            "findings": findings,
            "reviewers": scoped_verdicts or dict(review.reviewers),
        }
    )
    scoping: dict[str, Any] = {
        "policy": SCOPING_POLICY,
        "demoted_findings": demotions,
        "reviewers_before": dict(review.reviewers),
        "reviewers_after": dict(scoped.reviewers),
    }
    return scoped, scoping