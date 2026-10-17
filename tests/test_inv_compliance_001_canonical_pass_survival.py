"""RED lock test for INV-COMPLIANCE-001: canonical-revision PASS survival.

INV-COMPLIANCE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L170 [4. Global invariants]):
A PASS bound to one canonical revision SHALL survive a later canonical revision only when
current evidence is re-established or deterministic evidence-applicability rules prove that the
supporting evidence remains valid for the later revision.

The codebase has no primitive that reports whether a compliance PASS survives a later canonical
revision. These deterministic offline tests therefore lock the not-yet-existing pure primitive
``converge_orchestrator.compliance.ComplianceEngine.pass_survives_canonical_revision``:

- against the old implementation the class has no such symbol, so every test fails with the
  literal marker ``COMPLIANCE001_CANONICAL_PASS_SURVIVAL_MISSING``;
- once the primitive is added, the same tests must pass. It takes a PASS's bound canonical
  revision, the later canonical revision under consideration, whether current evidence is
  re-established against that later revision, the supporting evidence units (each bound to a
  revision), and the set of evidence units declared revision-independent by deterministic
  applicability rules, and returns a boolean:

  * re-established disjunct -- survival is True when current evidence is re-established
    against the later revision, with no applicability proof required;
  * applicability disjunct -- when nothing is re-established, survival is True only when every
    supporting evidence unit is either bound to the later revision or declared
    revision-independent by a deterministic applicability rule;
  * fail-closed -- when nothing is re-established and any supporting evidence unit is neither
    bound to the later revision nor revision-independent, survival is False.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain mappings.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import converge_orchestrator.compliance as compliance

_MISSING = "COMPLIANCE001_CANONICAL_PASS_SURVIVAL_MISSING"
_NAME = "pass_survives_canonical_revision"

# A pure survival primitive:
# (bound_revision, later_revision, reestablished, evidence, revision_independent) -> bool.
Survival = Callable[..., bool]


def _primitive() -> Survival:
    """Resolve the INV-COMPLIANCE-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``ComplianceEngine`` has no ``pass_survives_canonical_revision`` symbol) each test fails
    with the literal ``COMPLIANCE001_CANONICAL_PASS_SURVIVAL_MISSING`` marker rather than an
    opaque AttributeError.
    """
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, (
        f"{_MISSING}: converge_orchestrator.compliance.ComplianceEngine.{_NAME} is not defined; "
        "INV-COMPLIANCE-001 requires a deterministic, in-memory compliance-PASS survival "
        "primitive that reports survival across a later canonical revision only when current "
        "evidence is re-established or deterministic evidence-applicability rules prove the "
        "supporting evidence remains valid for the later revision."
    )
    return cast(Survival, method)


def _evidence(*units: tuple[str, str | None]) -> list[dict[str, object]]:
    """Build supporting evidence units as plain mappings: ``{id, bound_revision}``."""
    return [{"id": ident, "bound_revision": revision} for ident, revision in units]


def test_reestablished_evidence_survives_without_applicability_proof() -> None:
    """Disjunct 1: re-established evidence survives with no applicability proof required."""
    primitive = _primitive()
    # Evidence is bound to the original revision only and nothing is declared
    # revision-independent, so the applicability disjunct alone would fail-closed; survival
    # here must come solely from the re-established disjunct.
    result = primitive(
        bound_revision="rev-1",
        later_revision="rev-2",
        reestablished=True,
        evidence=_evidence(("ev-1", "rev-1"), ("ev-2", "rev-1")),
        revision_independent=frozenset(),
    )
    assert result is True


def test_applicability_disjunct_all_units_bound_or_independent() -> None:
    """Disjunct 2: every unit bound to the later revision or declared revision-independent."""
    primitive = _primitive()
    # ev-1 and ev-3 are bound to the later revision; ev-2 is bound elsewhere but is declared
    # revision-independent by a deterministic applicability rule. Nothing is re-established.
    result = primitive(
        bound_revision="rev-1",
        later_revision="rev-2",
        reestablished=False,
        evidence=_evidence(("ev-1", "rev-2"), ("ev-2", "rev-1"), ("ev-3", "rev-2")),
        revision_independent=frozenset({"ev-2"}),
    )
    assert result is True


def test_fail_closes_when_unproven_evidence_is_not_reestablished() -> None:
    """Fail-closed: unproven evidence with no re-establishment blocks survival."""
    primitive = _primitive()
    # ev-1 is bound only to the original revision and is NOT revision-independent, so it is
    # not proven applicable to the later revision, and nothing is re-established.
    result = primitive(
        bound_revision="rev-1",
        later_revision="rev-2",
        reestablished=False,
        evidence=_evidence(("ev-1", "rev-1"), ("ev-2", "rev-2")),
        revision_independent=frozenset({"ev-2"}),
    )
    assert result is False
