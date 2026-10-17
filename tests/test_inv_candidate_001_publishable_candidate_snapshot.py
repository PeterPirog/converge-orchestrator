"""RED lock test for INV-CANDIDATE-001: candidate-snapshot identity binding.

INV-CANDIDATE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L168 [4. Global invariants]):
Scope, quality, verifier, risk, review, and integration-readiness evidence for a candidate
SHALL apply to one exact publishable candidate snapshot and candidate content identity rather
than to an incomplete textual patch or mutable ambient working directory.

The codebase has no primitive that derives one candidate content identity from the complete
publishable candidate snapshot bytes and records whether scope, quality, verifier, risk, review,
and integration-readiness evidence apply only to that exact identity. These deterministic offline
tests therefore lock the not-yet-existing primitive
``converge_orchestrator.evidence.CandidateSnapshotIdentity``:

- against the old implementation the module has no such symbol, so every test fails with the
  literal marker ``CANDIDATE001_CANDIDATE_SNAPSHOT_IDENTITY_MISSING``;
- once the primitive is added, the same tests must pass: the identity is derived
  deterministically from the complete snapshot bytes (identical bytes -> the same identity,
  different bytes -> a different identity); evidence applies only when its identity equals that
  exact snapshot identity (evidence tied to a partial patch or a mutable ambient directory does
  not apply); every required category (scope, quality, verifier, risk, review,
  integration_readiness) not bound to the exact snapshot identity is reported unbound, so a
  fully bound snapshot is reported complete/applicable.

Identity hashes are SHA-256 hex digests, consistent with the existing evidence code (e.g.
``hashlib.sha256(...).hexdigest()``). No network, model, provider, subprocess, or ambient-directory
reads: the checks are pure, deterministic in-memory operations.
"""

from __future__ import annotations

import hashlib

import converge_orchestrator.evidence as evidence

_MISSING = "CANDIDATE001_CANDIDATE_SNAPSHOT_IDENTITY_MISSING"

_REQUIRED_CATEGORIES = ("scope", "quality", "verifier", "risk", "review", "integration_readiness")

_FULL_SNAPSHOT = b'{"run_id":"run-1","task_id":"task-1","payload":"v1"}\n'
_PARTIAL_PATCH = b"incomplete textual patch fragment"
_AMBIENT_DIR = b"mutable ambient working directory state"


def _sha256(value: bytes) -> str:
    """Deterministic SHA-256 candidate content identity digest (64 lowercase hex chars)."""
    return hashlib.sha256(value).hexdigest()


def _snapshot_identity() -> type:
    """Resolve the INV-CANDIDATE-001 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(evidence, "CandidateSnapshotIdentity", None)
    assert primitive is not None, (
        f"{_MISSING}: converge_orchestrator.evidence.CandidateSnapshotIdentity is not defined; "
        "INV-CANDIDATE-001 requires scope, quality, verifier, risk, review, and "
        "integration-readiness evidence to apply to one exact publishable candidate snapshot and "
        "candidate content identity rather than to an incomplete textual patch or mutable "
        "ambient working directory."
    )
    return primitive


def test_identity_derived_deterministically_from_complete_snapshot_bytes() -> None:
    SnapshotIdentity = _snapshot_identity()
    first = SnapshotIdentity(_FULL_SNAPSHOT)
    second = SnapshotIdentity(_FULL_SNAPSHOT)
    assert first.identity_sha256 == second.identity_sha256
    assert first.identity_sha256 == _sha256(_FULL_SNAPSHOT)
    different = SnapshotIdentity(b'{"run_id":"run-1","task_id":"task-1","payload":"v2"}\n')
    assert different.identity_sha256 != first.identity_sha256


def test_evidence_applies_only_when_identity_equals_snapshot_identity() -> None:
    SnapshotIdentity = _snapshot_identity()
    identity = SnapshotIdentity(_FULL_SNAPSHOT)
    exact = _sha256(_FULL_SNAPSHOT)
    assert exact == identity.identity_sha256
    assert identity.applies_to(exact) is True
    partial_patch = _sha256(_PARTIAL_PATCH)
    ambient_dir = _sha256(_AMBIENT_DIR)
    assert partial_patch != exact
    assert ambient_dir != exact
    assert identity.applies_to(partial_patch) is False
    assert identity.applies_to(ambient_dir) is False


def test_categories_not_bound_to_exact_identity_reported_unbound() -> None:
    SnapshotIdentity = _snapshot_identity()
    identity = SnapshotIdentity(_FULL_SNAPSHOT)
    exact = identity.identity_sha256
    foreign = _sha256(_PARTIAL_PATCH)
    # Every required category bound to a foreign (partial-patch) identity: all unbound.
    mismatched = {category: foreign for category in _REQUIRED_CATEGORIES}
    report = identity.evaluate(mismatched)
    assert tuple(report.unbound) == _REQUIRED_CATEGORIES
    assert report.complete is False
    # Only the category not bound to the exact identity is reported unbound.
    partially_bound = {category: exact for category in _REQUIRED_CATEGORIES}
    partially_bound["scope"] = _sha256(b"stale scope evidence from a prior snapshot")
    report_partial = identity.evaluate(partially_bound)
    assert tuple(report_partial.unbound) == ("scope",)
    assert report_partial.complete is False


def test_all_required_categories_bound_reported_complete() -> None:
    SnapshotIdentity = _snapshot_identity()
    identity = SnapshotIdentity(_FULL_SNAPSHOT)
    exact = identity.identity_sha256
    fully_bound = {category: exact for category in _REQUIRED_CATEGORIES}
    report = identity.evaluate(fully_bound)
    assert report.complete is True
    assert tuple(report.unbound) == ()
    assert set(report.applied) == set(_REQUIRED_CATEGORIES)
