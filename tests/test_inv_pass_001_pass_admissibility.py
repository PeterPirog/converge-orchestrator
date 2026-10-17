"""RED lock test for INV-PASS-001: mandatory PASS admissibility decision.

INV-PASS-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L172 [4. Global invariants]):
A mandatory requirement SHALL NOT enter or remain PASS unless an explicit PASS admissibility
decision identifies the exact requirement evidence policy and evidence set that authorize PASS
for the applicable canonical revision.

The codebase has no primitive that decides whether a mandatory requirement may enter or remain
PASS. These deterministic offline tests therefore lock the not-yet-existing pure primitive
``converge_orchestrator.compliance.ComplianceEngine.mandatory_pass_admissible``:

- against the old implementation the class has no such symbol, so every test fails with the
  literal marker ``PASS001_PASS_ADMISSIBILITY_MISSING``;
- once the primitive is added, the same tests must pass. It takes whether the requirement is
  mandatory, the requirement status, the explicit PASS admissibility decision (``None`` when no
  decision is present), and the applicable canonical revision under consideration, and returns a
  boolean:

  * admission disjunct -- a non-mandatory requirement, or a mandatory requirement that is not in
    PASS, is admitted (True) with no admissibility decision required;
  * admissible decision -- a mandatory requirement entering or remaining PASS is admitted (True)
    only when an explicit decision identifies the exact requirement evidence policy, the exact
    evidence set, and a canonical revision equal to the applicable canonical revision;
  * fail-closed -- a mandatory requirement entering or remaining PASS is denied (False) when the
    decision is absent, the evidence policy is not identified, the evidence set is not
    identified, or the decision is bound to a different canonical revision.

No network, model, provider, subprocess, git, or filesystem side effects: the checks are pure,
deterministic in-memory operations over plain values and mappings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

import converge_orchestrator.compliance as compliance
from converge_orchestrator.models import RequirementStatus

_MISSING = "PASS001_PASS_ADMISSIBILITY_MISSING"
_NAME = "mandatory_pass_admissible"

# A pure admissibility primitive:
# (mandatory, status, decision, applicable_revision) -> bool.
Admissibility = Callable[..., bool]


def _primitive() -> Admissibility:
    """Resolve the INV-PASS-001 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``ComplianceEngine`` has no ``mandatory_pass_admissible`` symbol) each test fails with the
    literal ``PASS001_PASS_ADMISSIBILITY_MISSING`` marker rather than an opaque AttributeError.
    """
    method = getattr(compliance.ComplianceEngine, _NAME, None)
    assert method is not None, (
        f"{_MISSING}: converge_orchestrator.compliance.ComplianceEngine.{_NAME} is not defined; "
        "INV-PASS-001 requires a deterministic, in-memory mandatory PASS admissibility primitive "
        "that admits a mandatory requirement into or as PASS only when an explicit PASS "
        "admissibility decision identifies the exact requirement evidence policy, the exact "
        "evidence set, and the applicable canonical revision."
    )
    return cast(Admissibility, method)


def _decision(
    *,
    evidence_policy: str | None = "POLICY-REQ-1",
    evidence: tuple[str, ...] | None = ("ev-1", "ev-2"),
    canonical_revision: str | None = "rev-1",
) -> Mapping[str, object]:
    """Build an explicit PASS admissibility decision as a plain mapping.

    Any field left ``None`` is omitted from the decision so that the decision does not identify
    that element; this is how the fail-closed cases express a missing evidence policy, a missing
    evidence set, or a decision bound to a different canonical revision.
    """
    decision: dict[str, object] = {}
    if evidence_policy is not None:
        decision["evidence_policy"] = evidence_policy
    if evidence is not None:
        decision["evidence"] = list(evidence)
    if canonical_revision is not None:
        decision["canonical_revision"] = canonical_revision
    return decision


def test_non_mandatory_pass_admitted_without_decision() -> None:
    """Admission disjunct: a non-mandatory PASS needs no admissibility decision."""
    primitive = _primitive()
    result = primitive(
        mandatory=False,
        status=RequirementStatus.PASS,
        decision=None,
        applicable_revision="rev-1",
    )
    assert result is True


def test_mandatory_non_pass_admitted_without_decision() -> None:
    """Admission disjunct: a mandatory non-PASS needs no admissibility decision."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.FAIL,
        decision=None,
        applicable_revision="rev-1",
    )
    assert result is True


def test_mandatory_pass_without_decision_fails_closed() -> None:
    """Fail-closed: a mandatory PASS with no admissibility decision is denied."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.PASS,
        decision=None,
        applicable_revision="rev-1",
    )
    assert result is False


def test_mandatory_pass_missing_evidence_policy_fails_closed() -> None:
    """Fail-closed: a decision that does not identify the evidence policy is denied."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.PASS,
        decision=_decision(evidence_policy=None),
        applicable_revision="rev-1",
    )
    assert result is False


def test_mandatory_pass_missing_evidence_set_fails_closed() -> None:
    """Fail-closed: a decision that does not identify the evidence set is denied."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.PASS,
        decision=_decision(evidence=None),
        applicable_revision="rev-1",
    )
    assert result is False


def test_mandatory_pass_bound_to_other_revision_fails_closed() -> None:
    """Fail-closed: a decision bound to a different canonical revision is denied."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.PASS,
        decision=_decision(canonical_revision="rev-2"),
        applicable_revision="rev-1",
    )
    assert result is False


def test_mandatory_pass_with_complete_decision_admitted() -> None:
    """Admissible decision: a complete decision bound to the applicable revision is admitted."""
    primitive = _primitive()
    result = primitive(
        mandatory=True,
        status=RequirementStatus.PASS,
        decision=_decision(),
        applicable_revision="rev-1",
    )
    assert result is True
