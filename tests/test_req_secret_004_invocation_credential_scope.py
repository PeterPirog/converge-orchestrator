"""REQ-SECRET-004: each invocation receives only credentials assigned to its role.

REQ-SECRET-004 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L224
[6. Project configuration and secret sources]): each agent or tool invocation
SHALL receive only credentials explicitly assigned to its role and operation,
and a role without credential authority SHALL NOT receive that credential
through environment inheritance, mounts, generated configuration, prompts, or
tool configuration.

These offline, deterministic tests pin that contract against the pure,
fail-closed evaluator
``converge_orchestrator.config.evaluate_invocation_credential_scope`` over a
state mapping carrying:

* ``role`` -- the invocation's non-blank string role;
* ``operation`` -- the invocation's non-blank string operation;
* ``credential_assignments`` -- the explicit list of credential assignments,
  each entry a mapping carrying a non-blank string ``credential`` (the
  credential name), a non-blank string ``role``, and a non-blank string
  ``operation``; and
* ``invocation_surfaces`` -- the delivery-surface mapping carrying exactly the
  five surface keys ``environment``, ``mounts``, ``generated_configuration``,
  ``prompt``, and ``tool_configuration``, each a list of non-blank
  credential-name strings.

A credential delivered on a surface is authorized only when a
``credential_assignment`` exists whose ``credential``, ``role``, and
``operation`` are all identical to the delivered credential name and the
invocation's role and operation. The evaluator reports one
``credential_not_assigned_to_role_operation:<surface>:<credential>`` clause for
every delivered credential lacking such an assignment, in fixed surface order
(environment, mounts, generated_configuration, prompt, tool_configuration) with
credential names sorted within each surface, and returns an empty tuple when
every delivered credential is authorized.

It fails closed with ``ValueError`` for a non-mapping state, a missing or
blank ``role`` or ``operation``, a missing or non-list
``credential_assignments``, a malformed assignment entry, a missing or
non-mapping ``invocation_surfaces``, a missing or unknown surface key, or a
surface value that is not a list of non-blank credential-name strings.

The evaluator never reads, echoes, persists, or compares credential values; it
inspects credential names only. No filesystem, environment, network, model,
git, or subprocess access occurs.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-SECRET-004-INVOCATION-CREDENTIAL-SCOPE-GUARD-MISSING`` instead of
silently accepting a credential delivered to a role and operation that were
never explicitly assigned to it.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure evaluator
# converge_orchestrator.config.evaluate_invocation_credential_scope is added.
# It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-004-INVOCATION-CREDENTIAL-SCOPE-GUARD-MISSING"

# The single clause prefix, fixed for this contract.
_CLAUSE = "credential_not_assigned_to_role_operation"

# The five delivery surfaces in their fixed report order.
_SURFACES = (
    "environment",
    "mounts",
    "generated_configuration",
    "prompt",
    "tool_configuration",
)

# A valid invocation role, operation, and one credential explicitly assigned to
# that exact role and operation.
_ROLE = "builder"
_OPERATION = "build"
_ASSIGNED = "ci_token"

# A sentinel for an intentionally absent key.
_MISSING = object()


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_invocation_credential_scope
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_invocation_credential_scope


def _clause(surface: str, credential: str) -> str:
    """Build the fixed clause for one delivered credential on one surface."""
    return f"{_CLAUSE}:{surface}:{credential}"


def _surfaces(**overrides: Any) -> dict[str, Any]:
    """A complete five-surface declaration; overrides replace individual surfaces."""
    surfaces = {surface: [] for surface in _SURFACES}
    surfaces.update(overrides)
    return surfaces


def _state(**overrides: Any) -> dict[str, Any]:
    """A well-formed invocation-credential-scope state; overrides replace keys."""
    state: dict[str, Any] = {
        "role": _ROLE,
        "operation": _OPERATION,
        "credential_assignments": [
            {"credential": _ASSIGNED, "role": _ROLE, "operation": _OPERATION},
        ],
        "invocation_surfaces": _surfaces(),
    }
    state.update(overrides)
    return state


def test_every_delivered_credential_assigned_is_accepted() -> None:
    # The single delivered credential is explicitly assigned to this exact role
    # and operation: the evaluator is satisfied and reports no clause.
    state = _state(invocation_surfaces=_surfaces(environment=[_ASSIGNED]))
    assert _guard()(state) == ()


def test_unassigned_credential_reports_clause() -> None:
    # A credential delivered through the environment is not assigned to this
    # role and operation: the evaluator reports the credential-scope clause for
    # that surface and no other.
    state = _state(invocation_surfaces=_surfaces(environment=["db_password"]))
    assert _guard()(state) == (_clause("environment", "db_password"),)


def test_clauses_reported_in_fixed_surface_order_and_sorted_within_surface() -> None:
    # Credentials are delivered across three surfaces out of sorted order on one
    # of them: the evaluator reports the environment clauses first (sorted), then
    # mounts, then prompt, in that fixed surface order.
    state = _state(
        invocation_surfaces=_surfaces(
            environment=["zeta", "alpha"],
            mounts=["gamma"],
            prompt=["beta"],
        )
    )
    assert _guard()(state) == (
        _clause("environment", "alpha"),
        _clause("environment", "zeta"),
        _clause("mounts", "gamma"),
        _clause("prompt", "beta"),
    )


def test_credential_assigned_to_different_role_or_operation_is_reported() -> None:
    # Both credentials are assigned to a different role or operation than this
    # invocation, so neither is authorized: the evaluator reports both, sorted.
    state = _state(
        credential_assignments=[
            {"credential": "db_password", "role": "reviewer", "operation": _OPERATION},
            {"credential": "api_token", "role": _ROLE, "operation": "deploy"},
        ],
        invocation_surfaces=_surfaces(environment=["db_password", "api_token"]),
    )
    assert _guard()(state) == (
        _clause("environment", "api_token"),
        _clause("environment", "db_password"),
    )


def test_assigned_credential_not_reported_among_unassigned() -> None:
    # A credential explicitly assigned to this exact role and operation is
    # authorized and not reported, while an unassigned one on the same surface is.
    state = _state(
        invocation_surfaces=_surfaces(environment=[_ASSIGNED, "db_password"]),
    )
    assert _guard()(state) == (_clause("environment", "db_password"),)


@pytest.mark.parametrize(
    "state",
    ["converge", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_state_fails_closed(state: Any) -> None:
    # A state that is not a mapping cannot be assessed at all: the evaluator
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(state)


@pytest.mark.parametrize(
    "role",
    [_MISSING, None, "", "   "],
    ids=["missing", "null", "empty", "blank"],
)
def test_missing_or_blank_role_fails_closed(role: Any) -> None:
    # A missing or blank invocation role is not a usable role/operation scope:
    # the evaluator fails closed with ValueError.
    state = _state()
    if role is _MISSING:
        del state["role"]
    else:
        state["role"] = role
    with pytest.raises(ValueError):
        _guard()(state)


@pytest.mark.parametrize(
    "operation",
    [_MISSING, None, "", "   "],
    ids=["missing", "null", "empty", "blank"],
)
def test_missing_or_blank_operation_fails_closed(operation: Any) -> None:
    # A missing or blank invocation operation is not a usable role/operation
    # scope: the evaluator fails closed with ValueError.
    state = _state()
    if operation is _MISSING:
        del state["operation"]
    else:
        state["operation"] = operation
    with pytest.raises(ValueError):
        _guard()(state)


def test_missing_credential_assignments_fails_closed() -> None:
    # Without an explicit credential-assignment list there is no authorization
    # fact to assess a delivered credential against: the evaluator fails closed.
    state = _state()
    del state["credential_assignments"]
    with pytest.raises(ValueError):
        _guard()(state)


def test_non_list_credential_assignments_fails_closed() -> None:
    # A credential-assignment list that is not a list is malformed: the
    # evaluator fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(credential_assignments=_ASSIGNED))


@pytest.mark.parametrize(
    "entry",
    [
        "ci_token",
        {"role": _ROLE, "operation": _OPERATION},
        {"credential": "   ", "role": _ROLE, "operation": _OPERATION},
        {"credential": "ci_token", "operation": _OPERATION},
        {"credential": "ci_token", "role": _ROLE},
        {"credential": "ci_token", "role": _ROLE, "operation": "   "},
    ],
    ids=[
        "non-mapping",
        "missing-credential",
        "blank-credential",
        "missing-role",
        "missing-operation",
        "blank-operation",
    ],
)
def test_malformed_assignment_entry_fails_closed(entry: Any) -> None:
    # An assignment entry that is not a mapping, or that is missing or blank in
    # any of credential/role/operation, is malformed: the evaluator fails closed
    # with ValueError rather than guessing the intent.
    with pytest.raises(ValueError):
        _guard()(_state(credential_assignments=[entry]))


def test_missing_invocation_surfaces_fails_closed() -> None:
    # Without an explicit delivery-surface declaration there is nothing to
    # assess: the evaluator fails closed with ValueError.
    state = _state()
    del state["invocation_surfaces"]
    with pytest.raises(ValueError):
        _guard()(state)


def test_non_mapping_invocation_surfaces_fails_closed() -> None:
    # A delivery-surface declaration that is not a mapping cannot carry the five
    # surface keys: the evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(invocation_surfaces=list(_SURFACES)))


def test_missing_surface_key_fails_closed() -> None:
    # A delivery-surface mapping that omits one of the five required surface
    # keys is incomplete: the evaluator fails closed with ValueError.
    surfaces = _surfaces()
    del surfaces["prompt"]
    with pytest.raises(ValueError):
        _guard()(_state(invocation_surfaces=surfaces))


def test_unknown_surface_key_fails_closed() -> None:
    # A delivery-surface mapping that declares a key beyond the five required
    # surfaces is malformed: the evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(invocation_surfaces=_surfaces(secret_store=["db_password"])))


def test_surface_value_not_a_list_fails_closed() -> None:
    # A surface whose value is not a list of credential names is malformed: the
    # evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(invocation_surfaces=_surfaces(environment=_ASSIGNED)))


@pytest.mark.parametrize(
    "credential",
    [None, "", "   ", 123],
    ids=["null", "empty", "blank", "number"],
)
def test_surface_non_blank_credential_name_fails_closed(credential: Any) -> None:
    # A surface entry that is not a non-blank credential-name string is
    # malformed: the evaluator fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(_state(invocation_surfaces=_surfaces(environment=[credential])))
