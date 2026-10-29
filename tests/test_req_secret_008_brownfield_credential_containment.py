"""REQ-SECRET-008: brownfield credential material stays contained while remediation is pending.

REQ-SECRET-008 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L228
[6. Project configuration and secret sources]): pre-existing credential
material discovered in a brownfield repository SHALL remain a visible
security/compliance gap and SHALL NOT be copied into prompts, generated
configuration, ordinary evidence payloads, diagnostics, or provider requests
while remediation is pending.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_brownfield_credential_material_containment``
over a run-state mapping carrying the single key:

* ``brownfield_credential_findings`` -- the explicit list of discovered
  pre-existing brownfield credential findings; each entry is a mapping
  carrying the identity-only fields:

  - ``identity`` -- a non-blank credential-material identity token;
  - ``remediation_pending`` -- an explicit boolean;
  - ``visible_compliance_gap`` -- an explicit boolean; and
  - ``affected_surfaces`` -- an explicit list of surface tokens, each one of
    the five declared surfaces, ``prompt``, ``generated_configuration``,
    ``ordinary_evidence_payload``, ``diagnostic``, and ``provider_request``.

The evaluator returns an empty tuple for an explicit empty finding list, and
for a finding whose remediation is not pending even when its gap is not
visible and surfaces were affected. While remediation is pending it reports,
in fixed order (gap-not-visible first, then the five surfaces in their fixed
order), identities sorted and deduplicated:

* ``credential_material_gap_not_visible:<identity>`` when the finding is not
  registered as a visible security/compliance gap; and
* ``credential_material_copied_into_<surface>:<identity>`` for each surface
  into which credential material was copied.

It fails closed with ``ValueError`` for a non-mapping state, a missing or
non-list ``brownfield_credential_findings``, a non-mapping finding, a blank
identity, a non-boolean ``remediation_pending`` or ``visible_compliance_gap``
flag, or a non-list or unknown ``affected_surfaces`` token.

The evaluator never reads, echoes, persists, or compares credential values;
clauses carry only identity and surface tokens. No filesystem, environment,
network, model, git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-008-BROWNFIELD-CREDENTIAL-MATERIAL-CONTAINMENT-GUARD-MISSING``
instead of silently accepting a brownfield credential finding that is neither
registered as a visible security/compliance gap nor withheld from the five
protected surfaces while remediation is pending.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_brownfield_credential_material_containment
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence that the guard is missing rather than that a candidate is compliant.
_GUARD_MISSING = "REQ-SECRET-008-BROWNFIELD-CREDENTIAL-MATERIAL-CONTAINMENT-GUARD-MISSING"

# The fixed clause stems in their fixed report order.
_CLAUSE_GAP_NOT_VISIBLE = "credential_material_gap_not_visible"
_CLAUSE_COPIED_INTO = "credential_material_copied_into"

# The five declared surfaces in their fixed report order.
_SURFACES = (
    "prompt",
    "generated_configuration",
    "ordinary_evidence_payload",
    "diagnostic",
    "provider_request",
)

# Credential-material identities, deliberately declared out of sorted order
# below so that a deliberate out-of-order declaration still yields a
# deterministic report.
_ID_ALPHA = "brownfield/credentials/aaa"
_ID_ZETA = "brownfield/credentials/zzz"

# A neutral, clearly non-secret placeholder used only to prove the evaluator
# reports identity and surface tokens only, never the credential material
# carried alongside a finding.
_NEUTRAL_PLACEHOLDER = "redacted-placeholder"

# A sentinel for an intentionally absent key.
_MISSING = object()


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import (
            evaluate_brownfield_credential_material_containment,
        )
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_brownfield_credential_material_containment


def _finding(**overrides: Any) -> dict[str, Any]:
    """A compliant pending finding: visible as a gap and copied into no surface.

    ``remediation_pending`` defaults to True so the finding is assessed, and
    ``visible_compliance_gap``/``affected_surfaces`` default to the compliant
    values so no clause is emitted unless a test opts in to a violation.
    """
    finding: dict[str, Any] = {
        "identity": _ID_ALPHA,
        "remediation_pending": True,
        "visible_compliance_gap": True,
        "affected_surfaces": [],
    }
    finding.update(overrides)
    return finding


def _state(*findings: dict[str, Any]) -> dict[str, Any]:
    """A run-state with an explicit list of brownfield credential findings."""
    return {"brownfield_credential_findings": list(findings)}


def test_empty_findings_returns_empty() -> None:
    # No pre-existing credential material was discovered: there is nothing to
    # contain, so the evaluator reports no clause.
    assert _guard()({"brownfield_credential_findings": []}) == ()


def test_compliant_pending_finding_returns_empty() -> None:
    # A pending finding registered as a visible security/compliance gap and not
    # copied into any surface: the containment contract is upheld, so no clause
    # is reported.
    assert _guard()(_state(_finding())) == ()


def test_not_pending_returns_empty_even_when_affected() -> None:
    # Remediation is not pending: the finding no longer needs containment, so
    # no clause is reported even though its gap is not visible and credential
    # material was copied into every surface.
    finding = _finding(
        remediation_pending=False,
        visible_compliance_gap=False,
        affected_surfaces=list(_SURFACES),
    )
    assert _guard()(_state(finding)) == ()


def test_gap_not_visible_reports_clause() -> None:
    # A pending finding not registered as a visible security/compliance gap:
    # exactly that identity clause is reported.
    finding = _finding(visible_compliance_gap=False)
    assert _guard()(_state(finding)) == (
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ALPHA}",
    )


def test_single_surface_reports_clause() -> None:
    # A pending finding copied into one surface (gap visible): exactly that
    # surface clause is reported.
    finding = _finding(affected_surfaces=["prompt"])
    assert _guard()(_state(finding)) == (
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
    )


def test_gap_and_surfaces_reported_in_fixed_order() -> None:
    # A pending finding both unregistered as a visible gap and copied into two
    # surfaces: the gap clause is reported first, then the surface clauses in
    # fixed surface order regardless of declaration order.
    finding = _finding(
        visible_compliance_gap=False,
        affected_surfaces=["diagnostic", "prompt"],
    )
    assert _guard()(_state(finding)) == (
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_diagnostic:{_ID_ALPHA}",
    )


def test_multiple_findings_sorted_and_deduplicated() -> None:
    # Two pending findings sharing the same gap and surface, declared out of
    # sorted order: the gap clauses are identity-sorted and the surface clauses
    # are identity-sorted and deduplicated.
    zeta = _finding(identity=_ID_ZETA, visible_compliance_gap=False, affected_surfaces=["prompt"])
    alpha = _finding(identity=_ID_ALPHA, visible_compliance_gap=False, affected_surfaces=["prompt"])
    assert _guard()(_state(zeta, alpha)) == (
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ALPHA}",
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ZETA}",
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ZETA}",
    )


def test_surface_order_is_fixed_across_surfaces() -> None:
    # A pending finding copied into all five surfaces, declared out of order:
    # the fixed surface order still governs the report.
    finding = _finding(
        affected_surfaces=[
            "provider_request",
            "ordinary_evidence_payload",
            "diagnostic",
            "generated_configuration",
            "prompt",
        ]
    )
    assert _guard()(_state(finding)) == (
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_generated_configuration:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_ordinary_evidence_payload:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_diagnostic:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_provider_request:{_ID_ALPHA}",
    )


def test_clauses_carry_only_identity_and_surface() -> None:
    # The finding carries credential material (a neutral placeholder) alongside
    # the identity: the evaluator reports only identity and surface tokens and
    # never the material in a returned clause.
    finding = _finding(
        visible_compliance_gap=False,
        affected_surfaces=["prompt"],
        credential_material=_NEUTRAL_PLACEHOLDER,
    )
    clauses = _guard()(_state(finding))
    assert clauses == (
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
    )
    for clause in clauses:
        assert _NEUTRAL_PLACEHOLDER not in clause


def test_duplicate_identity_and_surface_deduplicated() -> None:
    # Two pending findings sharing the same identity and the same surface: each
    # clause is emitted once, deduplicated, not once per finding.
    first = _finding(
        identity=_ID_ALPHA, visible_compliance_gap=False, affected_surfaces=["prompt"]
    )
    second = _finding(
        identity=_ID_ALPHA, visible_compliance_gap=False, affected_surfaces=["prompt"]
    )
    assert _guard()(_state(first, second)) == (
        f"{_CLAUSE_GAP_NOT_VISIBLE}:{_ID_ALPHA}",
        f"{_CLAUSE_COPIED_INTO}_prompt:{_ID_ALPHA}",
    )


@pytest.mark.parametrize(
    "state",
    ["findings", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_state_fails_closed(state: Any) -> None:
    # A run-state that is not a mapping cannot be assessed at all: the evaluator
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(state)


def test_missing_findings_fails_closed() -> None:
    # Without an explicit finding list there is nothing to assess: the evaluator
    # fails closed with ValueError rather than assuming no findings.
    with pytest.raises(ValueError):
        _guard()({})


@pytest.mark.parametrize(
    "findings",
    ["brownfield/credentials/aaa", {"identity": "brownfield/credentials/aaa"}, None],
    ids=["string", "mapping", "null"],
)
def test_non_list_findings_fails_closed(findings: Any) -> None:
    # A finding list that is not a list is malformed: the evaluator fails closed
    # with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()({"brownfield_credential_findings": findings})


@pytest.mark.parametrize(
    "finding",
    ["brownfield/credentials/aaa", None, 123],
    ids=["string", "null", "number"],
)
def test_non_mapping_finding_fails_closed(finding: Any) -> None:
    # A finding entry that is not a mapping is malformed: the evaluator fails
    # closed with ValueError rather than assuming an identity.
    with pytest.raises(ValueError):
        _guard()({"brownfield_credential_findings": [finding]})


@pytest.mark.parametrize(
    "identity",
    [None, "", "   "],
    ids=["null", "empty", "blank"],
)
def test_blank_identity_fails_closed(identity: Any) -> None:
    # A finding without a non-blank identity is malformed: the evaluator fails
    # closed with ValueError rather than assuming an identity.
    with pytest.raises(ValueError):
        _guard()(_state(_finding(identity=identity)))


@pytest.mark.parametrize(
    "flag",
    [_MISSING, None, "yes", 1],
    ids=["missing", "null", "string", "number"],
)
@pytest.mark.parametrize(
    "field",
    ["remediation_pending", "visible_compliance_gap"],
    ids=["remediation_pending", "visible_compliance_gap"],
)
def test_non_boolean_flag_fails_closed(field: str, flag: Any) -> None:
    # A containment flag that is absent, null, or non-boolean is malformed: the
    # evaluator fails closed with ValueError rather than guessing the intent.
    finding = _finding()
    if flag is _MISSING:
        del finding[field]
    else:
        finding[field] = flag
    with pytest.raises(ValueError):
        _guard()(_state(finding))


def test_non_list_affected_surfaces_fails_closed() -> None:
    # An affected-surfaces value that is not a list is malformed: the evaluator
    # fails closed with ValueError rather than silently passing.
    finding = _finding(affected_surfaces="prompt")
    with pytest.raises(ValueError):
        _guard()(_state(finding))


@pytest.mark.parametrize(
    "surface",
    [None, "", "environment", "repository_content"],
    ids=["null", "empty", "environment", "repository_content"],
)
def test_unknown_surface_token_fails_closed(surface: Any) -> None:
    # A surface token outside the five declared surfaces is unknown: the
    # evaluator fails closed with ValueError rather than guessing a category.
    finding = _finding(affected_surfaces=[surface])
    with pytest.raises(ValueError):
        _guard()(_state(finding))
