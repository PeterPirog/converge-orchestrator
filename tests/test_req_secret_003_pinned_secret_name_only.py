"""REQ-SECRET-003: pinned secret declarations carry name and source identity only.

REQ-SECRET-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L223
[6. Project configuration and secret sources]): the pinned run configuration
SHALL contain secret names and secret-source identity only, and deterministic
runtime code SHALL resolve secret values only for an operation with explicit
credential authority.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_pinned_secret_declaration`` over a
run-state mapping carrying:

* ``secret_declarations`` -- the pinned run-config secret declaration list, each
  entry a mapping carrying exactly a non-empty string ``name`` (the secret name)
  and a non-empty string ``source_identity`` (the secret-source identity), and
  nothing else; and
* ``operation`` -- the operation under consideration, a mapping carrying the
  explicit boolean ``resolves_secret_values`` (whether the operation requests
  secret-value resolution) and the explicit boolean
  ``has_explicit_credential_authority`` (whether it carries explicit credential
  authority).

The evaluator accepts (returns an empty tuple) a state whose declarations carry
only a secret name and a secret-source identity and whose operation either does
not request secret-value resolution or, when it does, carries explicit
credential authority.

The evaluator reports the fixed clause
``pinned_secret_declaration_contains_secret_value`` when a declaration carries
any field beyond ``name`` and ``source_identity``, and the fixed clause
``secret_value_resolution_without_explicit_credential_authority`` when the
operation requests secret-value resolution without explicit credential
authority (in that fixed order).

It fails closed with ``ValueError`` for a non-mapping state, a missing or
non-list ``secret_declarations``, a non-mapping declaration, a missing or blank
``name``, a missing or blank ``source_identity``, a missing or non-mapping
``operation``, or a non-boolean ``resolves_secret_values`` /
``has_explicit_credential_authority``.

The evaluator does not read, echo, persist, or compare secret values; it
inspects declaration shape and authority booleans only. No filesystem,
environment, network, model, git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-003-PINNED-SECRET-DECLARATION-GUARD-MISSING`` instead of silently
accepting a pinned declaration that carries a secret value or letting an
operation resolve secret values without explicit credential authority.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_pinned_secret_declaration is added.
# It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-003-PINNED-SECRET-DECLARATION-GUARD-MISSING"

# The two fixed clauses in their fixed report order.
_CLAUSE_CONTAINS_SECRET_VALUE = "pinned_secret_declaration_contains_secret_value"
_CLAUSE_RESOLUTION_WITHOUT_AUTHORITY = (
    "secret_value_resolution_without_explicit_credential_authority"
)

# A valid pinned declaration: exactly a secret name and a secret-source identity.
_NAME = "db_password"
_SOURCE_IDENTITY = "vault:prod/database"

# A neutral, clearly non-secret placeholder used only to prove the evaluator
# inspects declaration SHAPE (an extra field) and never the field's value.
_NEUTRAL_PLACEHOLDER = "redacted-placeholder"

_MISSING = object()


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_pinned_secret_declaration
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_pinned_secret_declaration


def _state(**overrides: Any) -> dict[str, Any]:
    """A run-state whose declaration carries only a name and a source identity."""
    state: dict[str, Any] = {
        "secret_declarations": [
            {"name": _NAME, "source_identity": _SOURCE_IDENTITY},
        ],
        "operation": {
            "resolves_secret_values": False,
            "has_explicit_credential_authority": False,
        },
    }
    state.update(overrides)
    return state


def test_declaration_carrying_only_name_and_source_identity_is_accepted() -> None:
    # The declaration carries exactly a secret name and a secret-source identity,
    # and the operation does not request secret-value resolution: the evaluator
    # is satisfied and reports no clause.
    assert _guard()(_state()) == ()


def test_value_resolution_with_explicit_credential_authority_is_accepted() -> None:
    # The operation requests secret-value resolution but carries explicit
    # credential authority: the evaluator is satisfied and reports no clause.
    state = _state(
        operation={
            "resolves_secret_values": True,
            "has_explicit_credential_authority": True,
        }
    )
    assert _guard()(state) == ()


def test_declaration_with_extra_field_reports_contains_secret_value() -> None:
    # The declaration carries a field beyond name and source identity (a secret
    # value): the evaluator reports the contains-secret-value clause and no other.
    state = _state(
        secret_declarations=[
            {
                "name": _NAME,
                "source_identity": _SOURCE_IDENTITY,
                "value": _NEUTRAL_PLACEHOLDER,
            },
        ]
    )
    assert _guard()(state) == (_CLAUSE_CONTAINS_SECRET_VALUE,)


def test_value_resolution_without_credential_authority_reports_clause() -> None:
    # The operation requests secret-value resolution without explicit credential
    # authority: the evaluator reports the without-authority clause and no other.
    state = _state(
        operation={
            "resolves_secret_values": True,
            "has_explicit_credential_authority": False,
        }
    )
    assert _guard()(state) == (_CLAUSE_RESOLUTION_WITHOUT_AUTHORITY,)


def test_both_clauses_reported_in_fixed_order() -> None:
    # The declaration carries a secret value AND the operation resolves values
    # without explicit credential authority: the evaluator reports the
    # contains-secret-value clause before the without-authority clause, in that
    # fixed order.
    state = _state(
        secret_declarations=[
            {
                "name": _NAME,
                "source_identity": _SOURCE_IDENTITY,
                "value": _NEUTRAL_PLACEHOLDER,
            },
        ],
        operation={
            "resolves_secret_values": True,
            "has_explicit_credential_authority": False,
        },
    )
    assert _guard()(state) == (_CLAUSE_CONTAINS_SECRET_VALUE, _CLAUSE_RESOLUTION_WITHOUT_AUTHORITY)


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


def test_missing_secret_declarations_fails_closed() -> None:
    # Without a pinned secret declaration list there is nothing to assess: the
    # evaluator fails closed with ValueError rather than silently passing.
    state = _state()
    del state["secret_declarations"]
    with pytest.raises(ValueError):
        _guard()(state)


def test_non_list_secret_declarations_fails_closed() -> None:
    # A secret declaration list that is not a list is malformed: the evaluator
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(secret_declarations="db_password"))


def test_non_mapping_declaration_fails_closed() -> None:
    # A declaration entry that is not a mapping cannot carry a name and a source
    # identity: the evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(secret_declarations=["db_password"]))


@pytest.mark.parametrize(
    "name",
    [_MISSING, None, "", "   "],
    ids=["missing", "null", "empty", "blank"],
)
def test_missing_or_blank_name_fails_closed(name: Any) -> None:
    # A declaration whose secret name is absent, null, or blank is not a usable
    # pinned declaration: the evaluator fails closed with ValueError.
    declaration: dict[str, Any] = {"source_identity": _SOURCE_IDENTITY}
    if name is not _MISSING:
        declaration["name"] = name
    with pytest.raises(ValueError):
        _guard()(_state(secret_declarations=[declaration]))


@pytest.mark.parametrize(
    "source_identity",
    [_MISSING, None, "", "   "],
    ids=["missing", "null", "empty", "blank"],
)
def test_missing_or_blank_source_identity_fails_closed(source_identity: Any) -> None:
    # A declaration whose secret-source identity is absent, null, or blank is not
    # a usable pinned declaration: the evaluator fails closed with ValueError.
    declaration: dict[str, Any] = {"name": _NAME}
    if source_identity is not _MISSING:
        declaration["source_identity"] = source_identity
    with pytest.raises(ValueError):
        _guard()(_state(secret_declarations=[declaration]))


def test_missing_operation_fails_closed() -> None:
    # Without an operation there is no credential-authority fact to assess a
    # value-resolution request against: the evaluator fails closed with ValueError.
    state = _state()
    del state["operation"]
    with pytest.raises(ValueError):
        _guard()(state)


def test_non_mapping_operation_fails_closed() -> None:
    # An operation that is not a mapping cannot carry the authority booleans: the
    # evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(operation="credential"))


@pytest.mark.parametrize(
    "resolves_secret_values",
    [_MISSING, None, "yes", 1],
    ids=["missing", "null", "string", "number"],
)
def test_non_boolean_resolution_flag_fails_closed(resolves_secret_values: Any) -> None:
    # A resolution flag that is absent, null, or non-boolean is malformed: the
    # evaluator fails closed with ValueError rather than guessing the intent.
    operation: dict[str, Any] = {"has_explicit_credential_authority": False}
    if resolves_secret_values is not _MISSING:
        operation["resolves_secret_values"] = resolves_secret_values
    with pytest.raises(ValueError):
        _guard()(_state(operation=operation))


@pytest.mark.parametrize(
    "has_explicit_credential_authority",
    [_MISSING, None, "yes", 1],
    ids=["missing", "null", "string", "number"],
)
def test_non_boolean_authority_flag_fails_closed(has_explicit_credential_authority: Any) -> None:
    # An authority flag that is absent, null, or non-boolean is malformed: the
    # evaluator fails closed with ValueError rather than guessing the intent.
    operation: dict[str, Any] = {"resolves_secret_values": True}
    if has_explicit_credential_authority is not _MISSING:
        operation["has_explicit_credential_authority"] = has_explicit_credential_authority
    with pytest.raises(ValueError):
        _guard()(_state(operation=operation))
