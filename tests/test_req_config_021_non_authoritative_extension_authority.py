"""REQ-CONFIG-021: a declared non-authoritative extension field must not alter
any authority surface.

REQ-CONFIG-021 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L219
[6. Project configuration and secret sources]): a declared non-authoritative
extension field SHALL NOT alter workflow authority, PASS semantics, mutation
authority, secret scope, sandbox policy, Git or remote integration semantics,
or release convergence until a later schema explicitly adopts that field as
authoritative policy.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard
``converge_orchestrator.config.evaluate_non_authoritative_extension_field_authority``.
The guard takes the extension (a mapping carrying a non-blank ``namespace`` and
a ``fields`` mapping of field name to a declaration whose ``alters`` value is a
list of authority-surface tokens) and the active schema (a mapping carrying
``adopted_authoritative_fields``, a list of ``"<namespace>.<field>"`` strings),
and returns the violated clauses in the fixed requirement surface order
(workflow_authority, pass_semantics, mutation_authority, secret_scope,
sandbox_policy, git_or_remote_integration_semantics, release_convergence), with
the extension fields within one surface reported in sorted qualified-field
order:

* the guard returns an empty tuple when no extension field alters any of the
  seven authority surfaces, so the surfaces are attributed to non-extension
  policy or to no extension field at all;
* the guard returns an empty tuple for a declared extension field that would
  alter a surface when that field is explicitly adopted by the active schema as
  ``"<namespace>.<field>"`` in ``adopted_authoritative_fields``;
* the guard reports exactly one
  ``non_authoritative_extension_field_alters_<surface>:<namespace>.<field>``
  clause per altered surface for a single un-adopted extension field that
  declares all seven surfaces, in the fixed requirement order;
* multiple offending extension fields within one surface are reported in
  sorted qualified-field order for deterministic results;
* the guard fails closed with ``ValueError`` on a non-mapping extension or
  active schema, a missing or blank extension namespace, missing or
  non-mapping extension fields, a non-list surface declaration, an unknown
  surface token, missing or non-list adopted fields, or a non-string
  adopted-field entry.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker
``REQ-CONFIG-021-NON-AUTHORITATIVE-EXTENSION-AUTHORITY-GUARD-MISSING``
instead of silently accepting a non-authoritative extension field that would
alter workflow authority, PASS semantics, mutation authority, secret scope,
sandbox policy, Git or remote integration semantics, or release convergence.
No network, model, git, or subprocess access occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_non_authoritative_extension_field_authority
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence.
_GUARD_MISSING = "REQ-CONFIG-021-NON-AUTHORITATIVE-EXTENSION-AUTHORITY-GUARD-MISSING"

# The seven authority surfaces in the fixed requirement order the guard must
# preserve when reporting altered-surface clauses.
_SURFACES = (
    "workflow_authority",
    "pass_semantics",
    "mutation_authority",
    "secret_scope",
    "sandbox_policy",
    "git_or_remote_integration_semantics",
    "release_convergence",
)


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import (
            evaluate_non_authoritative_extension_field_authority,
        )
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_non_authoritative_extension_field_authority


def _extension(namespace: Any = "vendor", fields: Any = None) -> dict[str, Any]:
    """Build an extension mapping from an optional namespace and fields mapping.

    Both parameters default to absent so the same helper can express a valid
    extension as well as the malformed (missing / non-mapping) shapes the
    fail-closed tests exercise.
    """
    extension: dict[str, Any] = {}
    if namespace is not None:
        extension["namespace"] = namespace
    if fields is not None:
        extension["fields"] = fields
    return extension


def _field(alters: Any = None) -> dict[str, Any]:
    """Build one extension-field declaration with an optional surface list."""
    declaration: dict[str, Any] = {}
    if alters is not None:
        declaration["alters"] = alters
    return declaration


def _active_schema(adopted: Any = None) -> dict[str, Any]:
    """Build an active schema from an optional adopted-field list.

    The parameter defaults to absent so the same helper can express a valid
    schema as well as the malformed (missing / non-list) shapes the fail-closed
    tests exercise.
    """
    schema: dict[str, Any] = {}
    if adopted is not None:
        schema["adopted_authoritative_fields"] = adopted
    return schema


def _evaluate(extension: Any, active_schema: Any) -> tuple[str, ...]:
    """Evaluate non-authoritative extension field authority through the guard."""
    return _guard()(extension, active_schema)


def _clause(surface: str, namespace: str, field: str) -> str:
    """The clause the guard reports for one altered surface by one field."""
    return f"non_authoritative_extension_field_alters_{surface}:{namespace}.{field}"


# --- Accepted configurations (no clause) ------------------------------------


def test_no_altered_surface_is_accepted() -> None:
    # An extension field that alters none of the seven authority surfaces is
    # attributed to ordinary non-extension policy: the guard reports no clause.
    ext = _extension("vendor", fields={"notes": _field([])})
    assert _evaluate(ext, _active_schema([])) == ()


def test_empty_fields_is_accepted() -> None:
    # An extension declaring no fields at all alters no authority surface: the
    # guard reports no clause.
    assert _evaluate(_extension("vendor", fields={}), _active_schema([])) == ()


def test_adopted_field_is_accepted() -> None:
    # A declared extension field that would alter a surface is explicitly
    # adopted by the active schema as "<namespace>.<field>": the guard accepts
    # it with no clause.
    ext = _extension("vendor", fields={"workflow_policy": _field(["workflow_authority"])})
    schema = _active_schema(["vendor.workflow_policy"])
    assert _evaluate(ext, schema) == ()


# --- Rejected configurations (clauses) ---------------------------------------


def test_single_field_altering_all_surfaces_reports_fixed_order() -> None:
    # One un-adopted extension field declaring all seven surfaces yields
    # exactly one clause per surface, in the fixed requirement order (not
    # alphabetical clause order).
    ext = _extension("vendor", fields={"policy": _field(list(_SURFACES))})
    assert _evaluate(ext, _active_schema([])) == tuple(
        _clause(surface, "vendor", "policy") for surface in _SURFACES
    )


def test_single_field_altering_one_surface_reports_clause() -> None:
    # The canonical violation: one un-adopted field altering a single surface
    # yields exactly its own clause and nothing else.
    ext = _extension("vendor", fields={"policy": _field(["pass_semantics"])})
    assert _evaluate(ext, _active_schema([])) == (
        _clause("pass_semantics", "vendor", "policy"),
    )


def test_multiple_fields_within_one_surface_reported_sorted() -> None:
    # Multiple offending fields within one surface are reported in sorted
    # qualified-field order, not insertion order.
    ext = _extension(
        "vendor",
        fields={
            "zzz": _field(["secret_scope"]),
            "aaa": _field(["secret_scope"]),
        },
    )
    assert _evaluate(ext, _active_schema([])) == (
        _clause("secret_scope", "vendor", "aaa"),
        _clause("secret_scope", "vendor", "zzz"),
    )


def test_fixed_surface_order_is_primary_across_fields() -> None:
    # Two fields alter two surfaces whose fixed requirement order differs from
    # alphabetical order; the fixed surface order is the primary clause order,
    # with fields within each surface in sorted qualified-field order.
    ext = _extension(
        "vendor",
        fields={
            "z": _field(["workflow_authority", "pass_semantics"]),
            "a": _field(["workflow_authority"]),
        },
    )
    assert _evaluate(ext, _active_schema([])) == (
        _clause("workflow_authority", "vendor", "a"),
        _clause("workflow_authority", "vendor", "z"),
        _clause("pass_semantics", "vendor", "z"),
    )


def test_mixed_adopted_and_unadopted_fields() -> None:
    # Two fields both alter workflow_authority; only the one that is not
    # adopted as "<namespace>.<field>" is reported.
    ext = _extension(
        "vendor",
        fields={
            "adopted": _field(["workflow_authority"]),
            "rogue": _field(["workflow_authority"]),
        },
    )
    schema = _active_schema(["vendor.adopted"])
    assert _evaluate(ext, schema) == (
        _clause("workflow_authority", "vendor", "rogue"),
    )


# --- Fail-closed (ValueError) ------------------------------------------------


@pytest.mark.parametrize(
    "extension",
    ["vendor", 123, ["vendor"], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_extension_fails_closed(extension: Any) -> None:
    # An extension that is not a mapping cannot be assessed: the guard fails
    # closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(extension, _active_schema([]))


@pytest.mark.parametrize(
    "active_schema",
    ["adopted", 123, ["adopted"], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_active_schema_fails_closed(active_schema: Any) -> None:
    # An active schema that is not a mapping cannot be assessed: the guard
    # fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(_extension("vendor", fields={}), active_schema)


def test_missing_namespace_fails_closed() -> None:
    # Without a namespace there is no qualified field to report: the guard
    # fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate({"fields": {}}, _active_schema([]))


@pytest.mark.parametrize(
    "namespace",
    [None, "", "   ", 123],
    ids=["null", "empty", "blank", "number"],
)
def test_blank_or_non_string_namespace_fails_closed(namespace: Any) -> None:
    # A null, empty, blank, or non-string namespace cannot qualify a field:
    # the guard fails closed with ValueError.
    extension = {"namespace": namespace, "fields": {}}
    with pytest.raises(ValueError):
        _evaluate(extension, _active_schema([]))


def test_missing_fields_fails_closed() -> None:
    # Without a fields mapping there are no extension fields to assess: the
    # guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(_extension("vendor"), _active_schema([]))


def test_non_mapping_fields_fails_closed() -> None:
    # A fields value that is not a mapping cannot enumerate fields: the guard
    # fails closed with ValueError.
    with pytest.raises(ValueError):
        _evaluate(
            {"namespace": "vendor", "fields": ["workflow_policy"]},
            _active_schema([]),
        )


def test_non_list_surface_declaration_fails_closed() -> None:
    # A field's surface declaration that is not a list cannot be enumerated:
    # the guard fails closed with ValueError.
    ext = _extension(
        "vendor",
        fields={"workflow_policy": {"alters": "workflow_authority"}},
    )
    with pytest.raises(ValueError):
        _evaluate(ext, _active_schema([]))


def test_unknown_surface_token_fails_closed() -> None:
    # A surface token outside the seven enumerated authority surfaces is
    # ambiguous: the guard fails closed with ValueError.
    ext = _extension("vendor", fields={"policy": {"alters": ["unauthorized_surface"]}})
    with pytest.raises(ValueError):
        _evaluate(ext, _active_schema([]))


def test_missing_adopted_fields_fails_closed() -> None:
    # Without an adopted_authoritative_fields list there is no adoption to
    # check against: the guard fails closed with ValueError.
    ext = _extension("vendor", fields={"policy": _field(["workflow_authority"])})
    with pytest.raises(ValueError):
        _evaluate(ext, _active_schema())


def test_non_list_adopted_fields_fails_closed() -> None:
    # An adopted_authoritative_fields value that is not a list cannot
    # enumerate adopted fields: the guard fails closed with ValueError.
    ext = _extension("vendor", fields={"policy": _field(["workflow_authority"])})
    with pytest.raises(ValueError):
        _evaluate(ext, _active_schema(adopted="vendor.policy"))


@pytest.mark.parametrize(
    "entry",
    [None, 123, ["vendor.policy"]],
    ids=["null", "number", "list"],
)
def test_non_string_adopted_field_entry_fails_closed(entry: Any) -> None:
    # A non-string entry in adopted_authoritative_fields cannot be matched:
    # the guard fails closed with ValueError.
    ext = _extension("vendor", fields={"policy": _field(["workflow_authority"])})
    with pytest.raises(ValueError):
        _evaluate(ext, _active_schema(adopted=[entry]))
