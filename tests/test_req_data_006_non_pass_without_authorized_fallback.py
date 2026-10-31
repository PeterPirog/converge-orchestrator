"""REQ-DATA-006: fail closed to non-PASS when no authorized complete-context fallback exists.

REQ-DATA-006 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L233
[6. Project configuration and secret sources]): When no authorized fallback can
receive the complete authoritative context required for the role, the operation
SHALL remain non-PASS rather than sending restricted content or weakening
authoritative context.

These offline, deterministic tests pin the pure evaluator
``converge_orchestrator.context.evaluate_authorized_fallback_non_pass``:

* it returns an empty tuple when at least one fallback endpoint declaration is
  authorized for the role and explicitly declares
  ``receives_complete_authoritative_context`` as ``True``;
* it returns the single non-PASS evidence clause
  ``no_authorized_fallback_receives_complete_authoritative_context:<role>`` when
  endpoints are empty, no endpoint lists the role, or every endpoint that lists
  the role declares incomplete context;
* malformed role values and malformed endpoint declarations fail closed with
  ``ValueError``.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-DATA-006-NO-AUTHORIZED-FALLBACK-NON-PASS-GUARD-MISSING`` until the guard
is added. No filesystem, network, model, provider, subprocess, git, or other
nondeterministic access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure evaluator is added. It is a literal, unique string that
# does not appear anywhere else in the repository.
_GUARD_MISSING = "REQ-DATA-006-NO-AUTHORIZED-FALLBACK-NON-PASS-GUARD-MISSING"


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.context import (
            evaluate_authorized_fallback_non_pass,
        )
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_authorized_fallback_non_pass


def _evaluate(role: Any, endpoints: Any) -> tuple[str, ...]:
    """Invoke the evaluator for the given role and endpoint declarations."""
    return _guard()(role, endpoints)


def _endpoint(
    name: str,
    authorized_roles: Any,
    receives_complete_authoritative_context: Any,
) -> dict[str, Any]:
    """Build one explicit fallback endpoint declaration."""
    return {
        "name": name,
        "authorized_roles": authorized_roles,
        "receives_complete_authoritative_context": receives_complete_authoritative_context,
    }


def test_authorized_complete_endpoint_yields_no_clause() -> None:
    endpoints = [_endpoint("complete", ("writer",), True)]
    assert _evaluate("writer", endpoints) == ()


def test_mixed_endpoints_with_one_complete_yields_no_clause() -> None:
    endpoints = [
        _endpoint("incomplete", ("writer",), False),
        _endpoint("complete", ("writer",), True),
        _endpoint("other_role", ("operator",), True),
    ]
    assert _evaluate("writer", endpoints) == ()


def test_empty_endpoints_returns_non_pass_clause() -> None:
    assert _evaluate("writer", []) == (
        "no_authorized_fallback_receives_complete_authoritative_context:writer",
    )


def test_no_endpoint_lists_role_returns_non_pass_clause() -> None:
    endpoints = [
        _endpoint("operator_only", ("operator",), True),
        _endpoint("incomplete_for_writer", ("writer",), False),
    ]
    assert _evaluate("writer", endpoints) == (
        "no_authorized_fallback_receives_complete_authoritative_context:writer",
    )


def test_all_role_endpoints_incomplete_returns_non_pass_clause() -> None:
    endpoints = [
        _endpoint("first", ("writer",), False),
        _endpoint("second", ("writer",), False),
    ]
    assert _evaluate("writer", endpoints) == (
        "no_authorized_fallback_receives_complete_authoritative_context:writer",
    )


@pytest.mark.parametrize(
    "bad_role",
    [None, 123, "", "   "],
    ids=["none", "int", "blank", "whitespace"],
)
def test_malformed_role_fails_closed(bad_role: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate(bad_role, [_endpoint("x", ("writer",), True)])


def test_non_iterable_endpoints_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate("writer", None)


@pytest.mark.parametrize("bad_endpoint", [None, "string", 123], ids=["none", "string", "int"])
def test_non_mapping_endpoint_fails_closed(bad_endpoint: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate("writer", [bad_endpoint])


def test_endpoint_missing_context_flag_fails_closed() -> None:
    endpoint = {"name": "x", "authorized_roles": ("writer",)}
    with pytest.raises(ValueError):
        _evaluate("writer", [endpoint])
