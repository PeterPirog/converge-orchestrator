"""REQ-CONFIG-020: unknown authoritative project-configuration fields are rejected.

REQ-CONFIG-020 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L218
[6. Project configuration and secret sources]): configuration validation SHALL
reject an unknown field that could be interpreted as control, security,
verification, authoring, recovery, integration, budget, or evidence policy
unless the active schema explicitly declares that field or declares its
namespace as non-authoritative extension data.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard
``converge_orchestrator.config.evaluate_unknown_authoritative_config_fields``.
The guard takes the configuration (a mapping of field names to values) and the
active schema (a mapping carrying ``declared_fields`` and
``non_authoritative_extension_namespaces``), and returns the violated clauses
in sorted deterministic order:

* the guard returns an empty tuple when every configuration field is either
  explicitly declared by the active schema (``declared_fields``), listed as a
  non-authoritative extension namespace
  (``non_authoritative_extension_namespaces``), or an unknown field whose name
  carries no authoritative policy domain;
* the guard reports exactly ``unknown_authoritative_config_field:<key>`` for a
  configuration field that is not declared by the active schema and not listed
  as a non-authoritative extension namespace, but whose name is interpretable
  as one of the eight authoritative policy domains: control, security,
  verification, authoring, recovery, integration, budget, or evidence;
* policy-domain detection is a case-insensitive snake_case/camelCase token
  match, so ``security_policy`` and ``securityPolicy`` are policy fields while
  ``custom_widget`` is not;
* multiple violations are reported in sorted deterministic clause order;
* the guard fails closed with ``ValueError`` on a non-mapping configuration or
  active schema, a missing or non-list ``declared_fields``, a non-list
  ``non_authoritative_extension_namespaces``, or a non-string namespace entry.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker
``REQ-CONFIG-020-UNKNOWN-AUTHORITATIVE-CONFIG-FIELD-GUARD-MISSING``
instead of silently accepting an unknown field whose name carries authoritative
control, security, verification, authoring, recovery, integration, budget, or
evidence policy. No network, model, git, or subprocess access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_unknown_authoritative_config_fields
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence.
_GUARD_MISSING = "REQ-CONFIG-020-UNKNOWN-AUTHORITATIVE-CONFIG-FIELD-GUARD-MISSING"

# The eight authoritative policy domains the guard must recognize in field names.
_POLICY_DOMAINS = (
    "control",
    "security",
    "verification",
    "authoring",
    "recovery",
    "integration",
    "budget",
    "evidence",
)


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_unknown_authoritative_config_fields
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_unknown_authoritative_config_fields


def _active_schema(
    declared_fields: Any = None,
    namespaces: Any = None,
) -> dict[str, Any]:
    """Build an active schema from its optional declared fields and namespaces.

    Both parameters default to absent so the same helper can express a valid
    schema as well as the malformed (missing / non-list) shapes the fail-closed
    tests exercise.
    """
    schema: dict[str, Any] = {}
    if declared_fields is not None:
        schema["declared_fields"] = declared_fields
    if namespaces is not None:
        schema["non_authoritative_extension_namespaces"] = namespaces
    return schema


def _evaluate(config: Any, active_schema: Any) -> tuple[str, ...]:
    """Evaluate unknown authoritative config fields through the guard."""
    return _guard()(config, active_schema)


def _clause(key: str) -> str:
    """The clause the guard reports for one unknown authoritative field."""
    return f"unknown_authoritative_config_field:{key}"


# --- Accepted configurations ------------------------------------------------


def test_undeclared_non_policy_field_is_accepted() -> None:
    # An unknown field whose name carries no policy domain is not policy
    # surface: the guard reports no clause for it.
    assert _evaluate({"custom_widget": True}, _active_schema([])) == ()


def test_declared_field_is_accepted() -> None:
    # A policy-bearing field is explicitly declared by the active schema:
    # the guard accepts it with no clause.
    assert _evaluate({"security_policy": True}, _active_schema(["security_policy"])) == ()


def test_policy_field_in_non_authoritative_namespace_is_accepted() -> None:
    # A policy-looking field is listed as non-authoritative extension data:
    # the guard accepts it with no clause.
    schema = _active_schema([], namespaces=["security_policy"])
    assert _evaluate({"security_policy": True}, schema) == ()


# --- Rejected configurations ------------------------------------------------


@pytest.mark.parametrize("domain", _POLICY_DOMAINS, ids=list(_POLICY_DOMAINS))
def test_undeclared_policy_domain_field_reports_clause(domain: str) -> None:
    # Each of the eight authoritative policy domains, appearing as an unknown
    # field, must be rejected with exactly its own clause.
    key = f"{domain}_setting"
    assert _evaluate({key: True}, _active_schema([])) == (_clause(key),)


def test_undeclared_security_policy_field_reports_clause() -> None:
    # The canonical acceptance case: an undeclared ``security_policy`` field
    # yields exactly its clause and nothing else.
    assert _evaluate({"security_policy": True}, _active_schema([])) == (
        "unknown_authoritative_config_field:security_policy",
    )


def test_camel_case_policy_field_reports_clause() -> None:
    # Policy-domain detection is a case-insensitive token match, so a camelCase
    # spelling of a policy domain is still an authoritative policy field.
    assert _evaluate({"securityPolicy": True}, _active_schema([])) == (
        "unknown_authoritative_config_field:securityPolicy",
    )


def test_multiple_violations_reported_in_sorted_order() -> None:
    # Multiple unknown policy-bearing fields are reported in sorted
    # deterministic clause order, not insertion order.
    config = {"security_policy": True, "budget_cap": True, "recovery_target": True}
    assert _evaluate(config, _active_schema([])) == (
        "unknown_authoritative_config_field:budget_cap",
        "unknown_authoritative_config_field:recovery_target",
        "unknown_authoritative_config_field:security_policy",
    )


# --- Fail-closed (ValueError) -----------------------------------------------


@pytest.mark.parametrize(
    "config",
    ["security_policy", 123, ["security_policy"], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_config_fails_closed(config: Any) -> None:
    # A configuration that is not a mapping cannot be assessed: the guard
    # fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(config, _active_schema([]))


@pytest.mark.parametrize(
    "active_schema",
    ["declared_fields", 123, ["declared_fields"], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_active_schema_fails_closed(active_schema: Any) -> None:
    # An active schema that is not a mapping cannot be assessed: the guard
    # fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate({"security_policy": True}, active_schema)


def test_missing_declared_fields_fails_closed() -> None:
    # Without a declared_fields list there is no declared field to check
    # against: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate({"security_policy": True}, _active_schema())


def test_non_list_declared_fields_fails_closed() -> None:
    # A declared_fields value that is not a list cannot enumerate declared
    # fields: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate({"security_policy": True}, _active_schema(declared_fields="security_policy"))


def test_non_list_extension_namespaces_fails_closed() -> None:
    # A non_authoritative_extension_namespaces value that is not a list cannot
    # enumerate permitted namespaces: the guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(
            {"security_policy": True},
            _active_schema(declared_fields=[], namespaces="security_policy"),
        )


@pytest.mark.parametrize(
    "namespace",
    [None, 123, ["security_policy"]],
    ids=["null", "number", "list"],
)
def test_non_string_namespace_entry_fails_closed(namespace: Any) -> None:
    # A non-string entry in the extension namespaces cannot be matched: the
    # guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(
            {"security_policy": True},
            _active_schema(declared_fields=[], namespaces=[namespace]),
        )
