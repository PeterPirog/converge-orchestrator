"""REQ-CONFIG-014: provider trust classes and repository-data egress are declared.

REQ-CONFIG-014 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L212
[6. Project configuration and secret sources]): project configuration SHALL define
provider trust classes and permitted repository-data egress for model, gateway,
MCP, and external analysis endpoints whose invocation can transmit project content.

These offline, deterministic tests pin that contract against the pure, fail-closed
guard ``converge_orchestrator.config.evaluate_provider_data_egress``:

* the guard returns an empty tuple for a complete policy declaring all four
  endpoint categories (model, gateway, mcp, external_analysis) with permitted
  repository-data egress only for local or trusted trust classes;
* the guard reports ``provider_trust_class_undeclared:<category>`` when a
  category's trust class is not explicitly declared;
* the guard reports ``repository_data_egress_undeclared:<category>`` when a
  category's repository-data egress is not explicitly declared;
* the guard reports
  ``repository_data_egress_permitted_to_untrusted_provider:<category>`` when a
  category permits repository-data egress to a trust class that is neither local
  nor trusted;
* the guard reports clauses in the fixed category order model, gateway, mcp,
  external_analysis, and, within a category, the trust-class clause before the
  egress clause;
* the guard fails closed with ``ValueError`` on a missing or empty
  ``provider_trust`` mapping, a missing or unknown endpoint category, or a
  non-mapping category declaration.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker ``REQ-CONFIG-014-PROVIDER-DATA-EGRESS-GUARD-MISSING``
instead of silently accepting a policy whose provider trust classes or
repository-data egress are under-declared. No network, model, provider,
subprocess, git, or filesystem access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.config.evaluate_provider_data_egress
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-CONFIG-014-PROVIDER-DATA-EGRESS-GUARD-MISSING"

# The four endpoint categories in the fixed order the requirement names them
# (model, gateway, mcp, external analysis).
_CATEGORIES = ("model", "gateway", "mcp", "external_analysis")

# A declared trust class that is neither local nor trusted; permitting
# repository-data egress to it is the violation the guard must report.
_UNTRUSTED_CLASS = "untrusted"


def _guard():
    """Return the guard, failing closed with the literal RED marker when it is absent."""
    try:
        from converge_orchestrator.config import evaluate_provider_data_egress
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_provider_data_egress


def _evaluate(policy: dict[str, Any]) -> tuple[str, ...]:
    """Evaluate provider trust/egress for ``policy`` through the guard."""
    return _guard()(policy)


def _declared(trust_class: str, egress: bool) -> dict[str, Any]:
    """One fully-declared endpoint category with an explicit trust class and egress."""
    return {"trust_class": trust_class, "repository_data_egress": egress}


def _complete_policy() -> dict[str, Any]:
    """A complete policy: all four categories declared, egress permitted only to trusted classes."""
    return {
        "provider_trust": {
            "model": _declared("local", True),
            "gateway": _declared("trusted", True),
            "mcp": _declared("local", False),
            "external_analysis": _declared("trusted", False),
        }
    }


def test_complete_policy_with_permitted_egress_only_for_trusted_classes_is_accepted() -> None:
    # All four endpoint categories are declared with explicit trust classes and
    # repository-data egress, and permitted egress is granted only to local or
    # trusted classes: the guard is satisfied and reports no clause.
    assert _evaluate(_complete_policy()) == ()


def test_undeclared_trust_class_reports_clause() -> None:
    # One category's trust class is not explicitly declared: the guard must report
    # the trust-class-undeclared clause for exactly that category.
    policy = _complete_policy()
    declaration = dict(policy["provider_trust"]["model"])
    del declaration["trust_class"]
    policy["provider_trust"]["model"] = declaration
    assert _evaluate(policy) == ("provider_trust_class_undeclared:model",)


def test_undeclared_repository_data_egress_reports_clause() -> None:
    # One category's repository-data egress is not explicitly declared: the guard
    # must report the egress-undeclared clause for exactly that category.
    policy = _complete_policy()
    declaration = dict(policy["provider_trust"]["gateway"])
    del declaration["repository_data_egress"]
    policy["provider_trust"]["gateway"] = declaration
    assert _evaluate(policy) == ("repository_data_egress_undeclared:gateway",)


def test_permitted_egress_to_untrusted_provider_reports_clause() -> None:
    # One category permits repository-data egress to a trust class that is neither
    # local nor trusted: the guard must report the permitted-to-untrusted clause.
    policy = _complete_policy()
    policy["provider_trust"]["mcp"] = _declared(_UNTRUSTED_CLASS, True)
    assert _evaluate(policy) == (
        "repository_data_egress_permitted_to_untrusted_provider:mcp",
    )


def test_undeclared_fields_report_clauses_in_fixed_within_category_order() -> None:
    # One category declares neither its trust class nor its egress: the guard must
    # report the trust-class clause before the egress clause for that category.
    policy = _complete_policy()
    declaration = dict(policy["provider_trust"]["model"])
    del declaration["trust_class"]
    del declaration["repository_data_egress"]
    policy["provider_trust"]["model"] = declaration
    assert _evaluate(policy) == (
        "provider_trust_class_undeclared:model",
        "repository_data_egress_undeclared:model",
    )


def test_clauses_reported_in_fixed_category_order() -> None:
    # Every category is missing its trust class at once: the guard must report all
    # trust-class-undeclared clauses in the fixed category order (model, gateway,
    # mcp, external_analysis).
    policy = {
        "provider_trust": {
            category: {"repository_data_egress": False} for category in _CATEGORIES
        }
    }
    assert _evaluate(policy) == tuple(
        f"provider_trust_class_undeclared:{category}" for category in _CATEGORIES
    )


def test_missing_provider_trust_mapping_fails_closed() -> None:
    # The provider_trust mapping is absent, so no endpoint category can be verified:
    # the guard must fail closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _evaluate({})


def test_empty_provider_trust_mapping_fails_closed() -> None:
    # The provider_trust mapping is empty, so no endpoint category is declared:
    # the guard must fail closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _evaluate({"provider_trust": {}})


@pytest.mark.parametrize("category", _CATEGORIES, ids=list(_CATEGORIES))
def test_missing_category_fails_closed(category: str) -> None:
    # One required endpoint category is missing from provider_trust: the guard
    # must fail closed with ValueError rather than silently accepting a gap.
    policy = _complete_policy()
    del policy["provider_trust"][category]
    with pytest.raises(ValueError):
        _evaluate(policy)


def test_unknown_category_fails_closed() -> None:
    # An endpoint category outside the four known categories is declared: the guard
    # must fail closed with ValueError on an unknown declaration.
    policy = _complete_policy()
    policy["provider_trust"]["custom_endpoint"] = _declared("local", False)
    with pytest.raises(ValueError):
        _evaluate(policy)


def test_non_mapping_category_declaration_fails_closed() -> None:
    # A category declaration is a non-mapping value, so its trust class and egress
    # cannot be read: the guard must fail closed with ValueError.
    policy = _complete_policy()
    policy["provider_trust"]["model"] = "local"
    with pytest.raises(ValueError):
        _evaluate(policy)
