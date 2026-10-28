"""REQ-SECRET-007: repository-derived content transfers are sanitized before transfer.

REQ-SECRET-007 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L227
[6. Project configuration and secret sources]): repository-derived content
selected for a model-backed role or external analysis tool SHALL pass
deterministic secret-detection and sanitization policy before transfer, and
detected credential material SHALL be withheld from that transfer while
preserving bounded evidence that content was withheld.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_repository_content_transfer_sanitization``
over a run-state mapping carrying the single key:

* ``repository_content_transfers`` -- the explicit list of repository-content
  transfer records selected for a model-backed role or external analysis tool.
  Each record is a mapping carrying:

  - ``surface`` -- one of the two declared surfaces, ``model_backed_role``
    or ``external_analysis_tool``;
  - ``identity`` -- a non-blank content identity token;
  - ``secret_detection_applied`` -- explicit boolean;
  - ``sanitization_applied`` -- explicit boolean;
  - ``credential_material_detected`` -- explicit boolean;
  - ``credential_material_withheld`` -- explicit boolean;
  - ``withholding_evidence`` -- when ``credential_material_detected`` is true,
    a mapping carrying a non-blank ``evidence_identity`` string and a ``bounded``
    flag that is explicitly true.

The evaluator returns an empty tuple for an explicit empty
``repository_content_transfers`` list, and for a compliant record that has
secret detection and sanitization applied, and -- when credential material was
detected -- withheld it with valid bounded evidence. It reports the violated
clauses in fixed surface order (``model_backed_role`` first, then
``external_analysis_tool``), identities sorted within each surface, and within
a record the fixed clause order:

* ``secret_detection_not_applied:<surface>:<identity>``;
* ``sanitization_not_applied:<surface>:<identity>``;
* ``credential_material_not_withheld:<surface>:<identity>``;
* ``credential_material_withheld_without_bounded_evidence:<surface>:<identity>``.

It fails closed with ``ValueError`` for a non-mapping state, a missing or
non-list ``repository_content_transfers``, a non-mapping record, an unknown
surface, a blank identity, a non-boolean policy flag, or a structurally
malformed (non-mapping) ``withholding_evidence`` when required.

The evaluator never reads, echoes, persists, or compares credential values;
clauses carry only the surface and content-identity tokens. No filesystem,
environment, network, model, git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-007-REPOSITORY-CONTENT-TRANSFER-SANITIZATION-GUARD-MISSING``
instead of silently accepting a transfer that forwards repository content to a
model-backed role or external analysis tool without the mandated
secret-detection, sanitization, withholding, and bounded-evidence guarantees.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_repository_content_transfer_sanitization
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence that the guard is missing rather than that a candidate is compliant.
_GUARD_MISSING = "REQ-SECRET-007-REPOSITORY-CONTENT-TRANSFER-SANITIZATION-GUARD-MISSING"

# The four fixed clauses in their fixed report order.
_CLAUSE_SECRET_DETECTION = "secret_detection_not_applied"
_CLAUSE_SANITIZATION = "sanitization_not_applied"
_CLAUSE_NOT_WITHHELD = "credential_material_not_withheld"
_CLAUSE_WITHOUT_EVIDENCE = "credential_material_withheld_without_bounded_evidence"

# The two declared surfaces in their fixed report order.
_SURFACE_MODEL_BACKED_ROLE = "model_backed_role"
_SURFACE_EXTERNAL_ANALYSIS = "external_analysis_tool"

# Content identities, deliberately declared out of sorted order below so that a
# deliberate out-of-order declaration still yields a deterministic report.
_ID_ALPHA = "repo/content/aaa.txt"
_ID_ZETA = "repo/content/zzz.txt"

# A neutral, clearly non-secret placeholder used only to prove the evaluator
# reports surface + identity tokens only, never the credential material carried
# alongside a transfer record.
_NEUTRAL_PLACEHOLDER = "redacted-placeholder"


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_repository_content_transfer_sanitization
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_repository_content_transfer_sanitization


def _evidence(evidence_identity: Any = "evidence-aaa", bounded: Any = True) -> dict[str, Any]:
    """One bounded-withholding-evidence declaration."""
    return {"evidence_identity": evidence_identity, "bounded": bounded}


def _record(**overrides: Any) -> dict[str, Any]:
    """A compliant transfer record: detection + sanitization applied, nothing detected.

    ``credential_material_detected`` defaults to False, so the withholding and
    evidence fields are inert unless a test opts in to the detected/withheld path.
    """
    record: dict[str, Any] = {
        "surface": _SURFACE_MODEL_BACKED_ROLE,
        "identity": _ID_ALPHA,
        "secret_detection_applied": True,
        "sanitization_applied": True,
        "credential_material_detected": False,
        "credential_material_withheld": True,
        "withholding_evidence": _evidence(),
    }
    record.update(overrides)
    return record


def _state(*records: dict[str, Any]) -> dict[str, Any]:
    """A run-state with an explicit list of transfer records."""
    return {"repository_content_transfers": list(records)}


def test_empty_transfers_returns_empty() -> None:
    # An explicit empty transfer list: nothing was selected for transfer, so the
    # evaluator is satisfied and reports no clause.
    assert _guard()({"repository_content_transfers": []}) == ()


def test_compliant_transfer_returns_empty() -> None:
    # Detection and sanitization applied, credential material detected and
    # withheld with valid bounded evidence: the evaluator is satisfied and the
    # no-clause result carries no credential material.
    record = _record(
        credential_material_detected=True,
        credential_material_withheld=True,
        withholding_evidence=_evidence("evidence-aaa", bounded=True),
    )
    assert _guard()(_state(record)) == ()


def test_secret_detection_not_applied_reports_clause() -> None:
    # Secret detection was not applied before transfer: exactly that clause,
    # scoped to the surface and identity, is reported.
    record = _record(secret_detection_applied=False)
    assert _guard()(_state(record)) == (
        f"{_CLAUSE_SECRET_DETECTION}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )


def test_sanitization_not_applied_reports_clause() -> None:
    # Sanitization was not applied before transfer: exactly that clause is
    # reported.
    record = _record(sanitization_applied=False)
    assert _guard()(_state(record)) == (
        f"{_CLAUSE_SANITIZATION}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )


def test_not_withheld_reports_clause() -> None:
    # Credential material was detected but not withheld from the transfer:
    # exactly the not-withheld clause is reported.
    record = _record(
        credential_material_detected=True,
        credential_material_withheld=False,
    )
    assert _guard()(_state(record)) == (
        f"{_CLAUSE_NOT_WITHHELD}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )


def test_withheld_bounded_not_true_reports_clause() -> None:
    # Credential material was detected and withheld, but the bounded flag is not
    # explicitly true: the bounded-evidence clause is reported.
    record = _record(
        credential_material_detected=True,
        credential_material_withheld=True,
        withholding_evidence=_evidence("evidence-aaa", bounded=False),
    )
    assert _guard()(_state(record)) == (
        f"{_CLAUSE_WITHOUT_EVIDENCE}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )


def test_withheld_blank_evidence_identity_reports_clause() -> None:
    # Credential material was detected and withheld, but the evidence identity
    # is blank: the bounded-evidence clause is reported.
    record = _record(
        credential_material_detected=True,
        credential_material_withheld=True,
        withholding_evidence=_evidence("   ", bounded=True),
    )
    assert _guard()(_state(record)) == (
        f"{_CLAUSE_WITHOUT_EVIDENCE}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )


def test_fixed_surface_order_model_backed_role_first() -> None:
    # The external-analysis transfer is declared first, but the fixed surface
    # order still reports the model-backed-role clause before the external
    # analysis clause.
    external = _record(
        surface=_SURFACE_EXTERNAL_ANALYSIS,
        identity=_ID_ZETA,
        secret_detection_applied=False,
    )
    model = _record(
        surface=_SURFACE_MODEL_BACKED_ROLE,
        identity=_ID_ALPHA,
        secret_detection_applied=False,
    )
    assert _guard()(_state(external, model)) == (
        f"{_CLAUSE_SECRET_DETECTION}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
        f"{_CLAUSE_SECRET_DETECTION}:{_SURFACE_EXTERNAL_ANALYSIS}:{_ID_ZETA}",
    )


def test_identities_sorted_within_surface() -> None:
    # Two records on the same surface, declared out of order: identities are
    # reported in sorted order within that surface.
    zeta = _record(identity=_ID_ZETA, sanitization_applied=False)
    alpha = _record(identity=_ID_ALPHA, sanitization_applied=False)
    assert _guard()(_state(zeta, alpha)) == (
        f"{_CLAUSE_SANITIZATION}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
        f"{_CLAUSE_SANITIZATION}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ZETA}",
    )


def test_clauses_carry_only_surface_and_identity() -> None:
    # The state carries credential material alongside a transfer record: the
    # evaluator reports only surface and identity tokens and never the material
    # in a returned clause.
    state = _state(
        _record(credential_material_detected=True, credential_material_withheld=False)
    )
    state["repository_content_transfers"][0]["content"] = _NEUTRAL_PLACEHOLDER
    clauses = _guard()(state)
    assert clauses == (
        f"{_CLAUSE_NOT_WITHHELD}:{_SURFACE_MODEL_BACKED_ROLE}:{_ID_ALPHA}",
    )
    for clause in clauses:
        assert _NEUTRAL_PLACEHOLDER not in clause


@pytest.mark.parametrize(
    "state",
    ["transfers", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_state_fails_closed(state: Any) -> None:
    # A run-state that is not a mapping cannot be assessed at all: the evaluator
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(state)


def test_missing_transfers_fails_closed() -> None:
    # Without an explicit transfer list there is nothing to assess: the
    # evaluator fails closed with ValueError rather than assuming no transfers.
    with pytest.raises(ValueError):
        _guard()({})


@pytest.mark.parametrize(
    "transfers",
    ["repo/content/aaa.txt", {"identity": "repo/content/aaa.txt"}, None],
    ids=["string", "mapping", "null"],
)
def test_non_list_transfers_fails_closed(transfers: Any) -> None:
    # A transfer list that is not a list is malformed: the evaluator fails
    # closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()({"repository_content_transfers": transfers})


@pytest.mark.parametrize(
    "record",
    ["repo/content/aaa.txt", None, 123],
    ids=["string", "null", "number"],
)
def test_non_mapping_record_fails_closed(record: Any) -> None:
    # A transfer record that is not a mapping is malformed: the evaluator fails
    # closed with ValueError rather than assuming a compliant transfer.
    with pytest.raises(ValueError):
        _guard()({"repository_content_transfers": [record]})


@pytest.mark.parametrize(
    "surface",
    [None, "", "role", "model_backed"],
    ids=["null", "empty", "role", "model_backed"],
)
def test_unknown_surface_fails_closed(surface: Any) -> None:
    # A surface outside the two declared surfaces is unknown: the evaluator
    # fails closed with ValueError rather than guessing a category.
    with pytest.raises(ValueError):
        _guard()(_state(_record(surface=surface)))


@pytest.mark.parametrize(
    "identity",
    [None, "", "   "],
    ids=["null", "empty", "blank"],
)
def test_blank_identity_fails_closed(identity: Any) -> None:
    # A transfer record without a non-blank identity is malformed: the
    # evaluator fails closed with ValueError rather than assuming an identity.
    with pytest.raises(ValueError):
        _guard()(_state(_record(identity=identity)))


@pytest.mark.parametrize(
    "flag",
    [
        "secret_detection_applied",
        "sanitization_applied",
        "credential_material_detected",
        "credential_material_withheld",
    ],
    ids=["secret_detection", "sanitization", "detected", "withheld"],
)
def test_non_boolean_policy_flag_fails_closed(flag: str) -> None:
    # A policy flag that is not an explicit boolean is malformed: the evaluator
    # fails closed with ValueError rather than guessing the intent.
    with pytest.raises(ValueError):
        _guard()(_state(_record(**{flag: "yes"})))


def test_non_mapping_withholding_evidence_fails_closed() -> None:
    # When credential material is detected and withheld, a structurally
    # malformed (non-mapping) withholding-evidence value is malformed state:
    # the evaluator fails closed with ValueError rather than assuming evidence.
    record = _record(
        credential_material_detected=True,
        credential_material_withheld=True,
        withholding_evidence="not-a-mapping",
    )
    with pytest.raises(ValueError):
        _guard()(_state(record))
