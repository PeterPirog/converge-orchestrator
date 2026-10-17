from __future__ import annotations

from collections.abc import Mapping

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

    @staticmethod
    def pass_survives_canonical_revision(
        bound_revision: str,
        later_revision: str,
        reestablished: bool,
        evidence: list[dict[str, object]],
        revision_independent: frozenset[str],
    ) -> bool:
        """Report whether a compliance PASS survives a later canonical revision.

        Implements INV-COMPLIANCE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L170):
        a PASS bound to ``bound_revision`` survives ``later_revision`` only when current
        evidence is re-established, or deterministic evidence-applicability rules prove that
        the supporting evidence remains valid for the later revision.

        This is a pure, deterministic in-memory predicate with no network, model, provider,
        subprocess, git, or filesystem side effects.

        ``evidence`` holds the supporting evidence units; each is a plain mapping with an
        ``id`` and the ``bound_revision`` the unit is bound to (``None`` when unbound).
        ``revision_independent`` holds the ids of evidence units declared
        revision-independent by a deterministic applicability rule.

        Returns ``True`` when ``reestablished`` is set (no applicability proof required),
        or when every evidence unit is either bound to ``later_revision`` or declared
        revision-independent. Otherwise fails closed and returns ``False``.
        """
        if reestablished:
            return True
        for unit in evidence:
            if unit.get("id") in revision_independent:
                continue
            if unit.get("bound_revision") == later_revision:
                continue
            return False
        return True

    @staticmethod
    def evidence_reusable_across_revisions(
        pinned_policy: Mapping[str, str],
        footprint: Mapping[str, str],
        reestablished_on_later_revision: bool,
    ) -> bool:
        """Report whether evidence may be reused on a later canonical revision.

        Implements INV-APPLICABILITY-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L171):
        cross-revision reuse of evidence is permitted only when an integrity-bound evidence
        dependency footprint is complete under the pinned evidence policy, or when the evidence
        is re-established on the later canonical revision; otherwise the evidence must stay
        revision-local.

        This is a pure, deterministic in-memory predicate with no network, model, provider,
        subprocess, git, or filesystem side effects.

        ``pinned_policy`` maps each required dependency id to the integrity digest mandated by
        the pinned evidence policy. ``footprint`` maps each dependency id to the integrity
        digest actually present in the integrity-bound dependency footprint.

        Returns ``True`` when ``reestablished_on_later_revision`` is set (no footprint required),
        or when every pinned-policy dependency is present in the footprint bound by the very
        digest the policy mandates. Otherwise fails closed and returns ``False`` so the
        evidence stays revision-local.
        """
        if reestablished_on_later_revision:
            return True
        for dependency_id, mandated_digest in pinned_policy.items():
            if footprint.get(dependency_id) != mandated_digest:
                return False
        return True

    @staticmethod
    def mandatory_pass_admissible(mandatory, status, decision, applicable_revision):
        if not mandatory or status != RequirementStatus.PASS:
            return True
        if not isinstance(decision, Mapping):
            return False
        if not decision.get("evidence_policy") or not decision.get("evidence"):
            return False
        return decision.get("canonical_revision") == applicable_revision

    @staticmethod
    def terminal_content_retrievable(
        recorded_source_identity: str | None,
        declared_source_identity: str | None,
        retrievable_from_recorded_source: bool,
        existing_only_in: frozenset[str],
    ) -> bool:
        """Report whether terminal convergence may depend on Git-native external content.

        Implements INV-MATERIALIZE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L173
        [4. Global invariants]): terminal convergence SHALL NOT depend on Git-native external
        content that exists only in a local cache, prior worktree, developer machine, model
        sandbox, CI cache, or another undeclared source and cannot be retrieved from the
        external source identity recorded for the canonical repository revision.

        This is a pure, deterministic in-memory predicate with no network, model, provider,
        subprocess, git, or filesystem side effects.

        ``recorded_source_identity`` is the external source identity recorded for the canonical
        repository revision (``None`` when none is recorded). ``declared_source_identity`` is
        the identity declared for the Git-native external content (``None`` when undeclared).
        ``retrievable_from_recorded_source`` reports whether the content can be retrieved from
        the recorded external source identity. ``existing_only_in`` names the sources the
        content exists only in (local cache, prior worktree, developer machine, model sandbox,
        CI cache, or other undeclared sources).

        Returns ``True`` only when the content can be retrieved from the recorded external
        source identity, or when a recorded identity exists and the content's declared identity
        matches it. Fails closed and returns ``False`` whenever the content exists only in a
        local cache, prior worktree, developer machine, model sandbox, CI cache, or another
        undeclared source, and whenever no external source identity is recorded (no vacuous
        identity match) -- so terminal convergence never depends on content confined to local
        or undeclared sources.
        """
        # Content confined to local/undeclared sources fails closed: it is not retrievable
        # from the external source identity recorded for the canonical repository revision, so
        # terminal convergence must not depend on it regardless of any other fact.
        if existing_only_in:
            return False
        # A retrievability fact only has meaning against a recorded external source identity;
        # with none recorded, the content cannot be retrieved from that identity.
        if retrievable_from_recorded_source and recorded_source_identity is not None:
            return True
        if (
            recorded_source_identity is not None
            and declared_source_identity is not None
            and declared_source_identity == recorded_source_identity
        ):
            return True
        return False
