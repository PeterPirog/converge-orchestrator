from __future__ import annotations

from .models import (
    ComplianceEntry,
    ComplianceSnapshot,
    Contract,
    RequirementStatus,
)


class ComplianceEngine:
    """Evidence-aware compliance state; verifier plugins can replace provisional evidence later."""

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
            entry.status = RequirementStatus.PARTIAL
            entry.evidence.extend(evidence)
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
            entry.evidence.extend(evidence)
        return updated
