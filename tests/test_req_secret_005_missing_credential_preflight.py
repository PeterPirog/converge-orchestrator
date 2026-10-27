"""REQ-SECRET-005: missing or unreadable credentials fail pre-external-side-effects.

REQ-SECRET-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L225
[6. Project configuration and secret sources]): missing or unreadable
credentials SHALL fail the dependent operation before external side effects,
and generated configuration or diagnostics SHALL identify missing credential
names without exposing values.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_missing_credential_preflight`` over a
run-state mapping carrying:

* ``required_credentials`` -- the explicit list of required credential names,
  each a non-blank string;
* ``credential_resolutions`` -- the explicit mapping of required credential
  name to a resolution entry carrying an explicit ``status`` token, one of
  ``available``, ``missing``, or ``unreadable``; and
* ``external_side_effects_started`` -- the explicit boolean indicating whether
  external side effects have already started for the dependent operation.

The evaluator accepts (returns an empty tuple) a state in which every required
credential resolves to ``available``. It reports one name-only clause per
required credential whose resolution status is not ``available``:

* ``missing_credential:<name>`` when the credential's resolution status is
  ``missing``;
* ``unreadable_credential:<name>`` when the credential's resolution status is
  ``unreadable``;

with the name-bearing clauses in sorted credential-name order. When at least
one required credential is missing or unreadable and
``external_side_effects_started`` is true, it additionally reports the fixed
clause ``failure_before_external_side_effects`` after the name-bearing clauses.

It fails closed with ``ValueError`` for a non-mapping state, a missing or
non-list ``required_credentials``, a blank credential name, a required
credential with no resolution entry, an unknown resolution ``status`` token, or
a non-boolean ``external_side_effects_started``.

The evaluator does not read, echo, persist, or compare credential values; it
inspects credential names and status tokens only. No filesystem, environment,
network, model, git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-005-MISSING-CREDENTIAL-PREFLIGHT-GUARD-MISSING`` instead of
silently accepting a missing or unreadable credential whose dependent operation
has nevertheless proceeded to external side effects.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_missing_credential_preflight is added.
# It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-005-MISSING-CREDENTIAL-PREFLIGHT-GUARD-MISSING"

# The fixed clauses in their fixed report order.
_CLAUSE_MISSING = "missing_credential"
_CLAUSE_UNREADABLE = "unreadable_credential"
_CLAUSE_FAILURE_BEFORE_SIDE_EFFECTS = "failure_before_external_side_effects"

# Two distinct required credential names, chosen so that a deliberate
# out-of-sorted-order declaration still yields a deterministic sorted report.
_NAME_ALPHA = "alpha_token"
_NAME_ZETA = "zeta_token"

# A neutral, clearly non-secret placeholder used only to prove the evaluator
# reports credential names and never the credential value or any other state
# value carried alongside the resolution.
_NEUTRAL_PLACEHOLDER = "redacted-placeholder"

# A sentinel for an intentionally absent key.
_MISSING = object()


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_missing_credential_preflight
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_missing_credential_preflight


def _state(**overrides: Any) -> dict[str, Any]:
    """A run-state in which every required credential resolves to ``available``."""
    state: dict[str, Any] = {
        "required_credentials": [_NAME_ALPHA, _NAME_ZETA],
        "credential_resolutions": {
            _NAME_ALPHA: {"status": "available"},
            _NAME_ZETA: {"status": "available"},
        },
        "external_side_effects_started": False,
    }
    state.update(overrides)
    return state


def test_all_available_no_side_effects_is_accepted() -> None:
    # Every required credential resolves to available: the evaluator is
    # satisfied and reports no clause.
    assert _guard()(_state()) == ()


def test_missing_credential_reports_name_clause() -> None:
    # One required credential resolves to missing: the evaluator reports the
    # missing-credential name clause and no other.
    state = _state(
        required_credentials=[_NAME_ALPHA],
        credential_resolutions={_NAME_ALPHA: {"status": "missing"}},
    )
    assert _guard()(state) == (f"{_CLAUSE_MISSING}:{_NAME_ALPHA}",)


def test_unreadable_credential_reports_name_clause() -> None:
    # One required credential resolves to unreadable: the evaluator reports the
    # unreadable-credential name clause and no other.
    state = _state(
        required_credentials=[_NAME_ZETA],
        credential_resolutions={_NAME_ZETA: {"status": "unreadable"}},
    )
    assert _guard()(state) == (f"{_CLAUSE_UNREADABLE}:{_NAME_ZETA}",)


def test_name_clauses_reported_in_sorted_name_order() -> None:
    # Two required credentials, declared out of sorted order, resolve to missing
    # and unreadable: the evaluator reports the name clauses in sorted
    # credential-name order (alpha before zeta), independent of declaration order.
    state = _state(
        required_credentials=[_NAME_ZETA, _NAME_ALPHA],
        credential_resolutions={
            _NAME_ZETA: {"status": "unreadable"},
            _NAME_ALPHA: {"status": "missing"},
        },
    )
    assert _guard()(state) == (
        f"{_CLAUSE_MISSING}:{_NAME_ALPHA}",
        f"{_CLAUSE_UNREADABLE}:{_NAME_ZETA}",
    )


def test_missing_credential_with_side_effects_reports_failure_clause() -> None:
    # A required credential resolves to missing and external side effects have
    # started: the evaluator reports the missing-credential name clause followed
    # by the failure-before-external-side-effects clause.
    state = _state(
        required_credentials=[_NAME_ALPHA],
        credential_resolutions={_NAME_ALPHA: {"status": "missing"}},
        external_side_effects_started=True,
    )
    assert _guard()(state) == (
        f"{_CLAUSE_MISSING}:{_NAME_ALPHA}",
        _CLAUSE_FAILURE_BEFORE_SIDE_EFFECTS,
    )


def test_unreadable_credential_with_side_effects_reports_failure_clause() -> None:
    # A required credential resolves to unreadable and external side effects have
    # started: the evaluator reports the unreadable-credential name clause
    # followed by the failure-before-external-side-effects clause.
    state = _state(
        required_credentials=[_NAME_ZETA],
        credential_resolutions={_NAME_ZETA: {"status": "unreadable"}},
        external_side_effects_started=True,
    )
    assert _guard()(state) == (
        f"{_CLAUSE_UNREADABLE}:{_NAME_ZETA}",
        _CLAUSE_FAILURE_BEFORE_SIDE_EFFECTS,
    )


def test_all_available_with_side_effects_reports_no_clause() -> None:
    # Every required credential resolves to available, so no missing or
    # unreadable credential exists to gate external side effects: the evaluator
    # reports no clause even though external side effects have started.
    assert _guard()(_state(external_side_effects_started=True)) == ()


def test_clauses_carry_only_credential_names() -> None:
    # The state carries a credential value (neutral placeholder) alongside the
    # resolution: the evaluator reports only the credential name and never the
    # value or any other state value in a returned clause.
    state = _state(
        required_credentials=[_NAME_ALPHA],
        credential_resolutions={
            _NAME_ALPHA: {"status": "missing", "value": _NEUTRAL_PLACEHOLDER},
        },
    )
    clauses = _guard()(state)
    assert clauses == (f"{_CLAUSE_MISSING}:{_NAME_ALPHA}",)
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


def test_missing_required_credentials_fails_closed() -> None:
    # Without an explicit required-credential list there is nothing to gate: the
    # evaluator fails closed with ValueError rather than silently passing.
    state = _state()
    del state["required_credentials"]
    with pytest.raises(ValueError):
        _guard()(state)


def test_non_list_required_credentials_fails_closed() -> None:
    # A required-credential list that is not a list is malformed: the evaluator
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(required_credentials=_NAME_ALPHA))


@pytest.mark.parametrize(
    "name",
    [None, "", "   "],
    ids=["null", "empty", "blank"],
)
def test_blank_required_credential_name_fails_closed(name: Any) -> None:
    # A required-credential entry that is not a non-blank credential-name string
    # is malformed: the evaluator fails closed with ValueError.
    state = _state(required_credentials=[name])
    with pytest.raises(ValueError):
        _guard()(state)


def test_missing_resolution_entry_fails_closed() -> None:
    # A required credential with no resolution entry has no usable status: the
    # evaluator fails closed with ValueError rather than assuming a status.
    state = _state(
        required_credentials=[_NAME_ALPHA, _NAME_ZETA],
        credential_resolutions={_NAME_ALPHA: {"status": "available"}},
    )
    with pytest.raises(ValueError):
        _guard()(state)


def test_unknown_status_token_fails_closed() -> None:
    # A resolution status token outside available/missing/unreadable is
    # malformed: the evaluator fails closed with ValueError rather than guessing.
    state = _state(
        required_credentials=[_NAME_ALPHA],
        credential_resolutions={_NAME_ALPHA: {"status": "rotated"}},
    )
    with pytest.raises(ValueError):
        _guard()(state)


@pytest.mark.parametrize(
    "flag",
    [_MISSING, None, "yes", 1],
    ids=["missing", "null", "string", "number"],
)
def test_non_boolean_side_effects_flag_fails_closed(flag: Any) -> None:
    # An external-side-effects flag that is absent, null, or non-boolean is
    # malformed: the evaluator fails closed with ValueError rather than guessing
    # the intent.
    state = _state()
    if flag is _MISSING:
        del state["external_side_effects_started"]
    else:
        state["external_side_effects_started"] = flag
    with pytest.raises(ValueError):
        _guard()(state)
