"""REQ-DATA-001: deterministic context assembly enforces provider trust classes.

REQ-DATA-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L230
[6. Project configuration and secret sources]): deterministic context assembly SHALL
enforce the configured provider trust class before transmitting repository-derived
content to any model, gateway, MCP server, or external analysis service.

These offline, deterministic tests pin the fail-closed context-assembly guard
``converge_orchestrator.context.evaluate_context_provider_trust``:

* non-repository-derived content returns an empty tuple without requiring a
  ``provider_trust`` declaration;
* repository-derived content is permitted only when its endpoint category declares
  ``trust_class`` as ``local`` or ``trusted`` and ``repository_data_egress`` is
  explicitly ``true``;
* the guard reports ``repository_derived_content_transmitted_to_untrusted_provider:
  <category>`` when repository-derived content targets a category whose trust class
  is not local or trusted;
* the guard reports ``repository_derived_content_egress_not_permitted:<category>``
  when repository-derived content targets a category whose ``repository_data_egress``
  is not explicitly ``true``;
* the guard reports ``context_provider_trust_class_undeclared:<category>`` when the
  category's ``trust_class`` is absent, blank, or not a string;
* the guard fails closed with ``ValueError`` on a missing or empty ``provider_trust``
  mapping, a missing or unknown endpoint category, or a non-mapping category
  declaration.

The guard does not exist yet, so every evaluation below fails with the literal TDD
RED evidence marker ``REQ-DATA-001-CONTEXT-PROVIDER-TRUST-GUARD-MISSING`` instead of
silently accepting repository-derived egress to an untrusted provider. No filesystem,
network, model, MCP, git, or subprocess access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure guard converge_orchestrator.context.evaluate_context_provider_trust
# is added. It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-DATA-001-CONTEXT-PROVIDER-TRUST-GUARD-MISSING"

_CATEGORIES = ("model", "gateway", "mcp", "external_analysis")
_UNTRUSTED_CLASS = "untrusted"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.context import evaluate_context_provider_trust
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_context_provider_trust


def _evaluate(
    repository_derived: bool,
    endpoint_category: str,
    provider_trust: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Evaluate the context provider-trust guard for a single content target."""
    return _guard()(repository_derived, endpoint_category, provider_trust)


def _declared(trust_class: str, egress: bool) -> dict[str, Any]:
    """One fully-declared endpoint category with an explicit trust class and egress."""
    return {"trust_class": trust_class, "repository_data_egress": egress}


def _complete_provider_trust() -> dict[str, Any]:
    """A complete provider_trust mapping: all four categories declared."""
    return {
        "model": _declared("local", True),
        "gateway": _declared("trusted", True),
        "mcp": _declared("local", False),
        "external_analysis": _declared("trusted", False),
    }


def test_non_repository_derived_content_passes_without_provider_trust() -> None:
    assert _evaluate(repository_derived=False, endpoint_category="model") == ()


def test_non_repository_derived_content_passes_with_empty_provider_trust() -> None:
    assert _evaluate(False, "model", {}) == ()


def test_repository_derived_local_with_egress_is_permitted() -> None:
    assert _evaluate(True, "model", _complete_provider_trust()) == ()


def test_repository_derived_trusted_with_egress_is_permitted() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["gateway"] = _declared("trusted", True)
    assert _evaluate(True, "gateway", provider_trust) == ()


def test_repository_derived_content_to_untrusted_provider_is_blocked() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["mcp"] = _declared(_UNTRUSTED_CLASS, True)
    assert _evaluate(True, "mcp", provider_trust) == (
        "repository_derived_content_transmitted_to_untrusted_provider:mcp",
    )


def test_repository_derived_content_with_egress_not_permitted_is_blocked() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["gateway"] = _declared("trusted", False)
    assert _evaluate(True, "gateway", provider_trust) == (
        "repository_derived_content_egress_not_permitted:gateway",
    )


def test_repository_derived_content_to_untrusted_with_no_egress_reports_both() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["external_analysis"] = _declared(_UNTRUSTED_CLASS, False)
    assert _evaluate(True, "external_analysis", provider_trust) == (
        "repository_derived_content_transmitted_to_untrusted_provider:external_analysis",
        "repository_derived_content_egress_not_permitted:external_analysis",
    )


@pytest.mark.parametrize(
    "trust_class",
    [None, "   ", 123],
    ids=["absent", "blank", "non_string"],
)
def test_undeclared_trust_class_reports_clause(trust_class: Any) -> None:
    provider_trust = _complete_provider_trust()
    declaration: dict[str, Any] = {"repository_data_egress": True}
    if trust_class is not None:
        declaration["trust_class"] = trust_class
    provider_trust["model"] = declaration
    assert _evaluate(True, "model", provider_trust) == (
        "context_provider_trust_class_undeclared:model",
    )


def test_missing_provider_trust_mapping_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate(True, "model")


def test_empty_provider_trust_mapping_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate(True, "model", {})


def test_missing_category_fails_closed() -> None:
    provider_trust = _complete_provider_trust()
    del provider_trust["mcp"]
    with pytest.raises(ValueError):
        _evaluate(True, "model", provider_trust)


def test_unknown_declared_category_fails_closed() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["custom_endpoint"] = _declared("local", False)
    with pytest.raises(ValueError):
        _evaluate(True, "model", provider_trust)


def test_unknown_target_category_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate(True, "custom_endpoint", _complete_provider_trust())


def test_non_mapping_category_declaration_fails_closed() -> None:
    provider_trust = _complete_provider_trust()
    provider_trust["model"] = "local"
    with pytest.raises(ValueError):
        _evaluate(True, "model", provider_trust)
