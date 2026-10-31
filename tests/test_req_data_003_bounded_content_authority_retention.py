"""REQ-DATA-003: guard bounded-content role invocations against loss of decision inputs.

REQ-DATA-003 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L234
[6. Project configuration and secret sources]): When project policy permits only a
subset of repository content to leave a trust boundary, the role invocation SHALL
either operate correctly with that bounded content or remain non-PASS;
sanitization or omission SHALL NOT remove authoritative information required
for a valid decision.

These offline, deterministic tests pin the pure evaluator
``converge_orchestrator.context.evaluate_bounded_content_authority_retention``:

* it returns an empty tuple when a bounded invocation retains every required
  authoritative item;
* it returns one ``bounded_content_omits_authoritative_information:<role>:<item>``
  clause per missing required authoritative item, sorted deterministically by
  item identifier, with no content values in the clause;
* it returns an empty tuple when ``bounded`` is explicitly ``False``, regardless
  of the difference between required and retained items;
* malformed ``role`` values, blank roles, non-boolean ``bounded`` flags, and
  malformed required/retained item collections fail closed with ``ValueError``.

The evaluator does not exist yet, so every evaluation below fails with the
literal TDD RED evidence marker
``REQ-DATA-003-BOUNDED-CONTENT-AUTHORITY-RETENTION-GUARD-MISSING`` until the
guard is added. No filesystem, network, model, provider, subprocess, git, or
other nondeterministic access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every failure
# below until the pure evaluator is added. It is a literal, unique string that
# does not appear anywhere else in the repository.
_GUARD_MISSING = "REQ-DATA-003-BOUNDED-CONTENT-AUTHORITY-RETENTION-GUARD-MISSING"


def _guard():
    """Return the evaluator, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.context import (
            evaluate_bounded_content_authority_retention,
        )
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_bounded_content_authority_retention


def _evaluate(
    role: Any,
    bounded: Any,
    required_authoritative_items: Any,
    retained_authoritative_items: Any,
) -> tuple[str, ...]:
    """Invoke the bounded-content authority-retention evaluator."""
    return _guard()(
        role,
        bounded,
        required_authoritative_items,
        retained_authoritative_items,
    )


def test_bounded_invocation_retains_all_required_items_yields_no_clause() -> None:
    required = ("source_requirements", "project_policy", "dependency_manifest")
    retained = {"source_requirements", "project_policy", "dependency_manifest"}
    assert _evaluate("architect", True, required, retained) == ()


def test_bounded_invocation_missing_one_required_item_returns_clause() -> None:
    required = ("source_requirements", "project_policy")
    retained = ("source_requirements",)
    assert _evaluate("architect", True, required, retained) == (
        "bounded_content_omits_authoritative_information:architect:project_policy",
    )


def test_bounded_invocation_missing_multiple_returns_sorted_clauses() -> None:
    required = ("z_last", "a_first", "m_middle")
    retained = ["m_middle"]
    assert _evaluate("reviewer", True, required, retained) == (
        "bounded_content_omits_authoritative_information:reviewer:a_first",
        "bounded_content_omits_authoritative_information:reviewer:z_last",
    )


def test_unbounded_invocation_produces_no_clause_regardless_of_missing_items() -> None:
    required = ("source_requirements",)
    retained = ()
    assert _evaluate("writer", False, required, retained) == ()


@pytest.mark.parametrize(
    "bad_role",
    [None, 123, "", "   "],
    ids=["none", "int", "blank", "whitespace"],
)
def test_malformed_role_fails_closed(bad_role: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate(bad_role, True, ("item",), ("item",))


@pytest.mark.parametrize(
    "bad_bounded",
    [None, 1, 0, "true"],
    ids=["none", "int_one", "int_zero", "string"],
)
def test_non_boolean_bounded_flag_fails_closed(bad_bounded: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", bad_bounded, ("item",), ("item",))


def test_required_items_non_iterable_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, None, ("item",))


def test_required_items_string_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, "item", ("item",))


def test_retained_items_non_iterable_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, ("item",), None)


def test_retained_items_string_fails_closed() -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, ("item",), "item")


@pytest.mark.parametrize(
    "bad_required",
    [("item", 123), ("",), ["  "]],
    ids=["non_string_member", "blank_item", "whitespace_item"],
)
def test_required_items_with_malformed_member_fails_closed(bad_required: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, bad_required, ("item",))


@pytest.mark.parametrize(
    "bad_retained",
    [("item", 123), ("",), ["  "]],
    ids=["non_string_member", "blank_item", "whitespace_item"],
)
def test_retained_items_with_malformed_member_fails_closed(bad_retained: Any) -> None:
    with pytest.raises(ValueError):
        _evaluate("architect", True, ("item",), bad_retained)
