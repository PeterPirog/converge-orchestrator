"""RED lock test for INV-EVID-001: candidate-bound evidence with drift invalidation.

INV-EVID-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L166 [4. Global invariants]):
Candidate-bound evidence SHALL remain valid only for the exact candidate content and policy
identity it evaluated, and later mutation of that content or policy SHALL invalidate affected
downstream evidence.

The codebase has no primitive that binds evidence to an exact candidate content hash and a
policy identity hash and invalidates it on drift. These deterministic offline tests therefore
lock the not-yet-existing primitive ``converge_orchestrator.evidence.CandidateBoundEvidence``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``EVID001_CANDIDATE_BOUND_EVIDENCE_MISSING``;
- once the primitive is added, the same tests must pass: bound evidence is valid only when both
  the candidate content hash and the policy identity hash still equal the values recorded at
  binding time, and is invalid as soon as either identity changes.

Identity hashes are SHA-256 digests, consistent with the existing evidence and cache code (e.g.
``architecture_policy_sha256`` and the ``candidate_sha256`` fingerprint convention). No network,
model, provider, or fetch operations: the checks are pure in-memory comparisons.
"""

from __future__ import annotations

import hashlib

import converge_orchestrator.evidence as evidence

_MISSING = "EVID001_CANDIDATE_BOUND_EVIDENCE_MISSING"


def _sha256(value: str) -> str:
    """Deterministic SHA-256 content/policy identity digest (64 lowercase hex chars)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bound_evidence() -> type:
    """Resolve the INV-EVID-001 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(evidence, "CandidateBoundEvidence", None)
    assert primitive is not None, (
        f"{_MISSING}: converge_orchestrator.evidence.CandidateBoundEvidence is not defined; "
        "INV-EVID-001 requires candidate-bound evidence that stays valid only for the exact "
        "candidate content hash and policy identity hash it evaluated."
    )
    return primitive


def test_bound_evidence_valid_only_when_both_identity_hashes_match() -> None:
    Bound = _bound_evidence()
    candidate = _sha256("candidate-content-v1")
    policy = _sha256("policy-identity-v1")
    bound = Bound(candidate, policy)
    assert bound.is_valid(candidate, policy) is True


def test_bound_evidence_invalid_after_candidate_content_hash_changes() -> None:
    Bound = _bound_evidence()
    candidate = _sha256("candidate-content-v1")
    policy = _sha256("policy-identity-v1")
    mutated_candidate = _sha256("candidate-content-v2")
    assert mutated_candidate != candidate
    bound = Bound(candidate, policy)
    assert bound.is_valid(mutated_candidate, policy) is False


def test_bound_evidence_invalid_after_policy_identity_hash_changes() -> None:
    Bound = _bound_evidence()
    candidate = _sha256("candidate-content-v1")
    policy = _sha256("policy-identity-v1")
    mutated_policy = _sha256("policy-identity-v2")
    assert mutated_policy != policy
    bound = Bound(candidate, policy)
    assert bound.is_valid(candidate, mutated_policy) is False
