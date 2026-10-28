"""REQ-SECRET-006: the secret-detection policy blocks integration on credential material.

REQ-SECRET-006 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L226
[6. Project configuration and secret sources]): the secret-detection policy
SHALL block integration when a candidate introduces credential material into
repository content or durable non-secret artifacts.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_secret_detection_policy`` over a
run-state mapping carrying:

* ``credential_material_findings`` -- the explicit list of credential-material
  findings; each entry is a mapping carrying the identity-only fields
  ``artifact_kind`` (one of the two declared kinds, ``repository_content``
  or ``durable_non_secret_artifact``) and ``identity`` (a non-blank artifact
  identity); and
* ``integration_authorized`` -- the explicit boolean indicating whether
  integration is authorized despite the reported findings.

The evaluator returns an empty tuple when ``credential_material_findings`` is
an explicit empty list, including when ``integration_authorized`` is true.
When findings exist it reports one identity-only clause per finding,
repository content first and then durable non-secret artifacts, identities
sorted within each kind:

* ``credential_material_in_repository_content:<identity>``;
* ``credential_material_in_durable_non_secret_artifact:<identity>``;

and, only when at least one finding exists and ``integration_authorized`` is
true, the fixed clause ``integration_not_blocked_by_secret_detection`` after
the finding clauses.

It fails closed with ``ValueError`` for a non-mapping state, a missing or
non-list ``credential_material_findings``, a non-mapping finding entry, an
unknown ``artifact_kind`` value, a blank artifact identity, or a non-boolean
``integration_authorized`` flag.

The evaluator does not read, echo, persist, or compare credential values; it
reports artifact identities only. No filesystem, environment, network, model,
git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-006-SECRET-DETECTION-POLICY-GUARD-MISSING`` instead of silently
accepting a candidate that introduces credential material into repository
content or durable non-secret artifacts while integration is nevertheless
authorized.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_secret_detection_policy is added.
# It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-006-SECRET-DETECTION-POLICY-GUARD-MISSING"

# The fixed clauses in their fixed report order.
_CLAUSE_REPOSITORY_CONTENT = "credential_material_in_repository_content"
_CLAUSE_DURABLE_ARTIFACT = "credential_material_in_durable_non_secret_artifact"
_CLAUSE_INTEGRATION_NOT_BLOCKED = "integration_not_blocked_by_secret_detection"

# The two declared artifact kinds in their fixed report order.
_KIND_REPOSITORY_CONTENT = "repository_content"
_KIND_DURABLE_ARTIFACT = "durable_non_secret_artifact"

# Artifact identities, deliberately declared out of sorted order below so that
# a deliberate out-of-order declaration still yields a deterministic report.
_ID_REPO_ZETA = "repo/zzz.txt"
_ID_REPO_ALPHA = "repo/aaa.txt"
_ID_ART_ZETA = "artifacts/zzz.yaml"
_ID_ART_ALPHA = "artifacts/aaa.yaml"

# A neutral, clearly non-secret placeholder used only to prove the evaluator
# reports artifact identities only, never credential material carried
# alongside the findings.
_NEUTRAL_PLACEHOLDER = "redacted-placeholder"

# A sentinel for an intentionally absent key.
_MISSING = object()


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_secret_detection_policy
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_secret_detection_policy


def _finding(kind: Any, identity: Any) -> dict[str, Any]:
    """One identity-only credential-material finding entry."""
    return {"artifact_kind": kind, "identity": identity}


def _state(**overrides: Any) -> dict[str, Any]:
    """A run-state with an explicit empty finding list and unauthorized integration."""
    state: dict[str, Any] = {
        "credential_material_findings": [],
        "integration_authorized": False,
    }
    state.update(overrides)
    return state


def test_empty_findings_not_authorized_is_accepted() -> None:
    # No credential material was found and integration is not authorized: the
    # evaluator is satisfied and reports no clause.
    assert _guard()(_state()) == ()


def test_empty_findings_authorized_is_accepted() -> None:
    # No credential material was found: integration is authorized, but there is
    # nothing to block, so the evaluator reports no clause.
    assert _guard()(_state(integration_authorized=True)) == ()


def test_repository_content_finding_reports_identity_clause() -> None:
    # One repository-content finding: the evaluator reports exactly that
    # identity clause and nothing else.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_REPOSITORY_CONTENT, _ID_REPO_ALPHA),
        ]
    )
    assert _guard()(state) == (f"{_CLAUSE_REPOSITORY_CONTENT}:{_ID_REPO_ALPHA}",)


def test_durable_artifact_finding_reports_identity_clause() -> None:
    # One durable non-secret artifact finding: the evaluator reports exactly
    # that identity clause and nothing else.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_DURABLE_ARTIFACT, _ID_ART_ALPHA),
        ]
    )
    assert _guard()(state) == (f"{_CLAUSE_DURABLE_ARTIFACT}:{_ID_ART_ALPHA}",)


def test_findings_reported_in_fixed_kind_and_sorted_identity_order() -> None:
    # Two findings of each kind, declared out of sorted order: the evaluator
    # reports the repository-content clauses first and the durable non-secret
    # artifact clauses after, identities sorted within each kind, independent
    # of declaration order.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_DURABLE_ARTIFACT, _ID_ART_ZETA),
            _finding(_KIND_REPOSITORY_CONTENT, _ID_REPO_ZETA),
            _finding(_KIND_REPOSITORY_CONTENT, _ID_REPO_ALPHA),
            _finding(_KIND_DURABLE_ARTIFACT, _ID_ART_ALPHA),
        ]
    )
    assert _guard()(state) == (
        f"{_CLAUSE_REPOSITORY_CONTENT}:{_ID_REPO_ALPHA}",
        f"{_CLAUSE_REPOSITORY_CONTENT}:{_ID_REPO_ZETA}",
        f"{_CLAUSE_DURABLE_ARTIFACT}:{_ID_ART_ALPHA}",
        f"{_CLAUSE_DURABLE_ARTIFACT}:{_ID_ART_ZETA}",
    )


def test_authorized_integration_reports_fixed_clause_after_findings() -> None:
    # Findings exist and integration is nevertheless authorized: the evaluator
    # reports the identity clauses followed by the fixed integration clause.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_REPOSITORY_CONTENT, _ID_REPO_ALPHA),
            _finding(_KIND_DURABLE_ARTIFACT, _ID_ART_ALPHA),
        ],
        integration_authorized=True,
    )
    assert _guard()(state) == (
        f"{_CLAUSE_REPOSITORY_CONTENT}:{_ID_REPO_ALPHA}",
        f"{_CLAUSE_DURABLE_ARTIFACT}:{_ID_ART_ALPHA}",
        _CLAUSE_INTEGRATION_NOT_BLOCKED,
    )


def test_unauthorized_integration_reports_no_integration_clause() -> None:
    # Findings exist but integration is not authorized: the policy is upheld,
    # so the evaluator reports only the identity clauses.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_DURABLE_ARTIFACT, _ID_ART_ALPHA),
        ],
        integration_authorized=False,
    )
    assert _guard()(state) == (f"{_CLAUSE_DURABLE_ARTIFACT}:{_ID_ART_ALPHA}",)


def test_clauses_carry_only_artifact_identities() -> None:
    # The state carries credential material (a neutral placeholder) alongside
    # the findings: the evaluator reports only artifact identities and never
    # the material in a returned clause.
    state = _state(
        credential_material_findings=[
            _finding(_KIND_REPOSITORY_CONTENT, _ID_REPO_ALPHA),
        ],
        credential_material=_NEUTRAL_PLACEHOLDER,
    )
    clauses = _guard()(state)
    assert clauses == (f"{_CLAUSE_REPOSITORY_CONTENT}:{_ID_REPO_ALPHA}",)
    for clause in clauses:
        assert _NEUTRAL_PLACEHOLDER not in clause


@pytest.mark.parametrize(
    "state",
    ["converge", 123, [], None],
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
    state = _state()
    del state["credential_material_findings"]
    with pytest.raises(ValueError):
        _guard()(state)


@pytest.mark.parametrize(
    "findings",
    ["repo/aaa.txt", {"identity": "repo/aaa.txt"}, None],
    ids=["string", "mapping", "null"],
)
def test_non_list_findings_fails_closed(findings: Any) -> None:
    # A finding list that is not a list is malformed: the evaluator fails closed
    # with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(credential_material_findings=findings))


@pytest.mark.parametrize(
    "entry",
    ["repo/aaa.txt", None, 123],
    ids=["string", "null", "number"],
)
def test_non_mapping_finding_entry_fails_closed(entry: Any) -> None:
    # A finding entry that is not a mapping is malformed: the evaluator fails
    # closed with ValueError rather than assuming an identity.
    with pytest.raises(ValueError):
        _guard()(_state(credential_material_findings=[entry]))


@pytest.mark.parametrize(
    "kind",
    [None, "", "repository", "secret_source"],
    ids=["null", "empty", "repository", "secret_source"],
)
def test_unknown_artifact_kind_fails_closed(kind: Any) -> None:
    # An artifact kind outside the two declared kinds is unknown: the evaluator
    # fails closed with ValueError rather than guessing a category.
    with pytest.raises(ValueError):
        _guard()(_state(credential_material_findings=[_finding(kind, _ID_REPO_ALPHA)]))


@pytest.mark.parametrize(
    "identity",
    [None, "", "   "],
    ids=["null", "empty", "blank"],
)
def test_blank_artifact_identity_fails_closed(identity: Any) -> None:
    # A finding entry without a non-blank artifact identity is malformed: the
    # evaluator fails closed with ValueError rather than assuming an identity.
    findings = [_finding(_KIND_REPOSITORY_CONTENT, identity)]
    with pytest.raises(ValueError):
        _guard()(_state(credential_material_findings=findings))


@pytest.mark.parametrize(
    "flag",
    [_MISSING, None, "yes", 1],
    ids=["missing", "null", "string", "number"],
)
def test_non_boolean_integration_authorized_fails_closed(flag: Any) -> None:
    # An integration-authorization flag that is absent, null, or non-boolean is
    # malformed: the evaluator fails closed with ValueError rather than guessing
    # the intent.
    state = _state()
    if flag is _MISSING:
        del state["integration_authorized"]
    else:
        state["integration_authorized"] = flag
    with pytest.raises(ValueError):
        _guard()(state)
