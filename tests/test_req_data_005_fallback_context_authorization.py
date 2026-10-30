"""REQ-DATA-005: skip fallback endpoints unauthorized for complete role context.

REQ-DATA-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L232
[6. Project configuration and secret sources]): A fallback endpoint that is not
authorized to receive the complete authoritative context required for the role
SHALL be skipped.

These offline, deterministic tests pin the pure selector
``converge_orchestrator.context.select_authorized_fallback_endpoints``:

* the selector returns, in declaration order, only fallback endpoint
declarations whose ``authorized_roles`` explicitly include the invoking role
and whose ``receives_complete_authoritative_context`` is explicitly ``True``;
* well-formed endpoints that do not list the role, or that declare
``receives_complete_authoritative_context`` as ``False``, are skipped rather
than returned;
* malformed role values (non-string, blank) and malformed endpoint declarations
(non-mapping, missing or malformed ``authorized_roles``/context flag) fail
closed with ``ValueError`` instead of being treated as authorized.

The selector does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-DATA-005-FALLBACK-CONTEXT-AUTHORIZATION-GUARD-MISSING`` instead of
silently selecting an unauthorized fallback endpoint. No filesystem, network,
model, MCP, git, or subprocess access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure selector converge_orchestrator.context.select_authorized_fallback_endpoints
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-DATA-005-FALLBACK-CONTEXT-AUTHORIZATION-GUARD-MISSING"


def _guard():
    """Return the selector, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.context import select_authorized_fallback_endpoints
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return select_authorized_fallback_endpoints


def _select(role: Any, endpoints: Any) -> tuple[dict[str, Any], ...]:
    """Invoke the fallback endpoint selector for the given role and declarations."""
    return _guard()(role, endpoints)


def _endpoint(
    name: str,
    authorized_roles: Any,
    receives_complete_authoritative_context: Any,
) -> dict[str, Any]:
    """Build one explicit endpoint declaration for use in selector tests."""
    return {
        "name": name,
        "authorized_roles": authorized_roles,
        "receives_complete_authoritative_context": receives_complete_authoritative_context,
    }


def _complete_endpoints() -> list[dict[str, Any]]:
    """A mixed list of fallback endpoint declarations covering all selection paths."""
    return [
        _endpoint("model_local", ("writer", "reviewer"), True),
        _endpoint("gateway_cloud", ("operator",), False),
        _endpoint("mcp_tool", ("writer", "operator"), True),
        _endpoint("external_analysis", ("reviewer",), True),
    ]


def test_authorized_complete_endpoints_returned_in_declaration_order() -> None:
    endpoints = [
        _endpoint("first", ("writer",), True),
        _endpoint("second", ("writer",), True),
    ]
    assert _select("writer", endpoints) == tuple(endpoints)


def test_endpoint_missing_role_is_skipped() -> None:
    endpoints = [
        _endpoint("allowed", ("writer",), True),
        _endpoint("not_allowed", ("operator",), True),
    ]
    assert _select("writer", endpoints) == (endpoints[0],)


def test_endpoint_with_incomplete_context_is_skipped() -> None:
    endpoints = [
        _endpoint("complete", ("writer",), True),
        _endpoint("incomplete", ("writer",), False),
    ]
    assert _select("writer", endpoints) == (endpoints[0],)


def test_mixed_endpoints_return_only_authorized_complete_in_order() -> None:
    endpoints = _complete_endpoints()
    expected = (endpoints[0], endpoints[2])  # writer role, complete context
    assert _select("writer", endpoints) == expected


def test_empty_endpoints_returns_empty() -> None:
    assert _select("writer", []) == ()


def test_no_matching_endpoints_returns_empty() -> None:
    endpoints = [
        _endpoint("operator_only", ("operator",), True),
        _endpoint("incomplete", ("writer",), False),
    ]
    assert _select("writer", endpoints) == ()


def test_duplicate_authorized_endpoints_preserve_declaration_order() -> None:
    endpoint = _endpoint("dup", ("writer",), True)
    endpoints = [endpoint, endpoint]
    assert _select("writer", endpoints) == (endpoint, endpoint)


@pytest.mark.parametrize(
    "bad_role",
    [None, 123, "", "   "],
    ids=["none", "int", "blank", "whitespace"],
)
def test_malformed_role_fails_closed(bad_role: Any) -> None:
    with pytest.raises(ValueError):
        _select(bad_role, [_endpoint("x", ("writer",), True)])


def test_non_iterable_endpoints_fails_closed() -> None:
    with pytest.raises(ValueError):
        _select("writer", None)


@pytest.mark.parametrize("bad_endpoint", [None, "string", 123], ids=["none", "string", "int"])
def test_non_mapping_endpoint_fails_closed(bad_endpoint: Any) -> None:
    with pytest.raises(ValueError):
        _select("writer", [bad_endpoint])


def test_endpoint_missing_authorized_roles_fails_closed() -> None:
    endpoint = {"name": "x", "receives_complete_authoritative_context": True}
    with pytest.raises(ValueError):
        _select("writer", [endpoint])


def test_endpoint_missing_context_flag_fails_closed() -> None:
    endpoint = {"name": "x", "authorized_roles": ("writer",)}
    with pytest.raises(ValueError):
        _select("writer", [endpoint])


@pytest.mark.parametrize(
    "bad_roles",
    [None, 123, ("writer", 123), ("",), ["  "]],
    ids=["none", "int", "non_string_member", "blank_role", "whitespace_role"],
)
def test_malformed_authorized_roles_fails_closed(bad_roles: Any) -> None:
    with pytest.raises(ValueError):
        _select("writer", [_endpoint("x", bad_roles, True)])


@pytest.mark.parametrize(
    "bad_flag",
    [None, "yes", 1, 0],
    ids=["none", "string", "int_one", "int_zero"],
)
def test_malformed_context_flag_fails_closed(bad_flag: Any) -> None:
    with pytest.raises(ValueError):
        _select("writer", [_endpoint("x", ("writer",), bad_flag)])
