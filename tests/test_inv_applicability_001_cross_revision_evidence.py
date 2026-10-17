"""RED lock test for INV-APPLICABILITY-001: cross-revision evidence reuse.

INV-APPLICABILITY-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L171 [4. Global invariants]):
Cross-revision reuse of evidence SHALL require an integrity-bound evidence dependency footprint
whose completeness is established under pinned evidence policy, or the evidence SHALL remain
revision-local and require re-establishment on the later canonical revision.

The codebase has no primitive that decides whether evidence bound to one canonical revision may
be reused on a later canonical revision. These deterministic offline tests therefore lock the
not-yet-existing pure primitive ``converge_orchestrator.compliance.ComplianceEngine
.evidence_reusable_across_revisions``:

- against the old implementation the class has no such symbol, so every test fails with the
  literal marker ``APPLICABILITY001_CROSS_REVISION_EVIDENCE_MISSING``;
- once the primitive is added, the same tests must pass. It takes the pinned evidence policy
  (a mapping of required dependency id -> mandated integrity digest), the integrity-bound
  dependency footprint (a mapping of dependency id -> integrity digest actually present), and a
  flag for whether the evidence is re-established on the later canonical revision, and returns a
  boolean:

  * re-establishment disjunct -- reuse is permitted when the evidence is re-established on the
    later revision, with no dependency footprint required;
  * footprint disjunct -- when nothing is re-established, reuse is permitted only when every
    dependency required by the pinned evidence policy is present in the integrity-bound
    footprint with a matching integrity digest;
  * fail-closed -- when nothing is re-established and a pinned-policy dependency is absent from
    the footprint or its integrity digest mismatches, reuse is denied so the evidence stays
    revision-local.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain mappings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import converge_orchestrator.compliance as compliance

_MISSING = "APPLICABILITY001_CROSS_REVISION_EVIDENCE_MISSING"
_NAME = "evidence_reusable_across_revisions"

# A pure applicability primitive:
# (pinned_policy, footprint, reestablished_on_later_revision) -> bool.
Applicability = Callable[..., bool]


def _primitive() -> Applicability:
    """Resolve the INV-APPLICABILITY-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``ComplianceEngine`` has no ``evidence_reusable_across_revisions`` symbol) each test fails
    with the literal ``APPLICABILITY001_CROSS_REVISION_EVIDENCE_MISSING`` marker rather than an
    opaque AttributeError.
    """
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, (
        f"{_MISSING}: converge_orchestrator.compliance.ComplianceEngine.{_NAME} is not defined; "
        "INV-APPLICABILITY-001 requires a deterministic, in-memory cross-revision evidence "
        "applicability primitive that permits reuse only when an integrity-bound dependency "
        "footprint is complete under a pinned evidence policy, or the evidence is re-established "
        "on the later canonical revision; otherwise the evidence must stay revision-local."
    )
    return cast(Applicability, method)


def test_reuse_permitted_when_reestablished_even_without_footprint() -> None:
    """Re-establishment disjunct: reuse is permitted with no footprint when re-established."""
    primitive = _primitive()
    # The pinned policy still mandates two dependencies, but nothing is supplied in the
    # footprint; survival of reuse here must come solely from the re-establishment disjunct.
    result = primitive(
        pinned_policy={"dep-a": "sha256:aaaa", "dep-b": "sha256:bbbb"},
        footprint={},
        reestablished_on_later_revision=True,
    )
    assert result is True


def test_reuse_permitted_when_footprint_complete_and_integrity_matched() -> None:
    """Footprint disjunct: every pinned-policy dependency is present with a matching digest."""
    primitive = _primitive()
    pinned_policy = {"dep-a": "sha256:aaaa", "dep-b": "sha256:bbbb"}
    # The footprint carries every dependency the pinned policy requires, each bound by the very
    # integrity digest the policy mandates; nothing is re-established.
    result = primitive(
        pinned_policy=pinned_policy,
        footprint=dict(pinned_policy),
        reestablished_on_later_revision=False,
    )
    assert result is True


def test_reuse_denied_when_required_dependency_absent() -> None:
    """Fail-closed: a pinned-policy dependency absent from the footprint blocks reuse."""
    primitive = _primitive()
    # dep-b is required by the pinned policy but missing from the footprint, and nothing is
    # re-established, so the evidence must stay revision-local.
    result = primitive(
        pinned_policy={"dep-a": "sha256:aaaa", "dep-b": "sha256:bbbb"},
        footprint={"dep-a": "sha256:aaaa"},
        reestablished_on_later_revision=False,
    )
    assert result is False


def test_reuse_denied_when_integrity_digest_mismatches() -> None:
    """Fail-closed: a present dependency whose integrity digest mismatches blocks reuse."""
    primitive = _primitive()
    # dep-a is present but bound by a different integrity digest than the pinned policy
    # mandates, and nothing is re-established, so the footprint is not integrity-complete.
    result = primitive(
        pinned_policy={"dep-a": "sha256:aaaa", "dep-b": "sha256:bbbb"},
        footprint={"dep-a": "sha256:WRONG", "dep-b": "sha256:bbbb"},
        reestablished_on_later_revision=False,
    )
    assert result is False
