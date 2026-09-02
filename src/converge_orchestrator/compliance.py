from __future__ import annotations

from .models import (
    ComplianceEntry,
    ComplianceSnapshot,
    Contract,
    Requirement,
    RequirementStatus,
    RequirementVerification,
)


class ComplianceEngine:
    """Evidence-aware compliance state with deterministic regression comparison."""

    @staticmethod
    def initial(contract: Contract) -> ComplianceSnapshot:
        return ComplianceSnapshot(
            entries={
                requirement.id: ComplianceEntry(
                    requirement_id=requirement.id,
                    status=RequirementStatus.UNVERIFIED,
                )
                for requirement in contract.requirements
            }
        )

    @staticmethod
    def apply_verifications(
        snapshot: ComplianceSnapshot,
        results: list[RequirementVerification],
    ) -> ComplianceSnapshot:
        updated = snapshot.model_copy(deep=True)
        for result in results:
            entry = updated.entries.get(result.requirement_id)
            if entry is None or result.status == RequirementStatus.UNVERIFIED:
                continue
            entry.status = result.status
            entry.evidence = list(dict.fromkeys([*entry.evidence, *result.evidence]))
        return updated

    @staticmethod
    def compare_mandatory_regressions(
        baseline: ComplianceSnapshot,
        candidate: ComplianceSnapshot,
        requirements: list[Requirement],
    ) -> int:
        mandatory_ids = {item.id for item in requirements if item.severity == "mandatory"}
        regressions = 0
        for requirement_id in mandatory_ids:
            before = baseline.entries.get(requirement_id)
            after = candidate.entries.get(requirement_id)
            if before is None or after is None:
                continue
            if before.status == RequirementStatus.PASS and after.status != RequirementStatus.PASS:
                regressions += 1
        return regressions

    @staticmethod
    def mark_local_pass(
        snapshot: ComplianceSnapshot,
        requirement_ids: list[str],
        evidence: list[str],
    ) -> ComplianceSnapshot:
        updated = snapshot.model_copy(deep=True)
        for requirement_id in requirement_ids:
            entry = updated.entries.get(requirement_id)
            if entry is None:
                continue
            if entry.status != RequirementStatus.PASS:
                entry.status = RequirementStatus.PARTIAL
            entry.evidence = list(dict.fromkeys([*entry.evidence, *evidence]))
        return updated

    @staticmethod
    def mark_remote_verified(
        snapshot: ComplianceSnapshot,
        requirement_ids: list[str],
        evidence: list[str],
    ) -> ComplianceSnapshot:
        updated = snapshot.model_copy(deep=True)
        for requirement_id in requirement_ids:
            entry = updated.entries.get(requirement_id)
            if entry is None:
                continue
            entry.status = RequirementStatus.PASS
            entry.evidence = list(dict.fromkeys([*entry.evidence, *evidence]))
        return updated
