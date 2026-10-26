"""REQ-CONFIG-019: shared control-state backend identity is recorded secret-safe.

REQ-CONFIG-019 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L217
[6. Project configuration and secret sources]): connection material for a
shared control-state backend SHALL remain secret-safe while Converge derives
and records a non-secret backend identity sufficient to distinguish backend
kind, logical database or namespace, authority generation, and any stable
service or cluster identity available to the deployment.

These offline, deterministic tests pin that contract against the pure,
fail-closed guard
``converge_orchestrator.config.evaluate_shared_control_state_backend_identity``
over a backend-identity declaration mapping carrying:

* ``backend_kind`` -- the backend kind (``postgres`` or ``sqlite``);
* ``logical_database_or_namespace`` -- the logical database or namespace;
* ``authority_generation`` -- a positive authority generation;
* ``stable_deployment_identity`` -- the optional stable service or cluster
  identity; its absence is permitted, and a present value must be a non-empty
  string;
* ``connection_material`` -- the secret-safe connection material, a non-empty
  mapping of non-empty string secret field names to non-empty string secret
  values; and
* ``recorded_backend_identity`` -- the non-secret backend identity that
  Converge derives and records, a non-empty mapping.

The guard accepts (returns an empty tuple) a complete postgres identity, and
a complete sqlite namespace identity without the optional stable service or
cluster identity.

The guard reports the fixed clauses in fixed order:
``backend_kind_undeclared`` when the backend kind is absent, null, non-string,
or blank; ``logical_database_or_namespace_undeclared`` when the logical
database or namespace is absent, null, non-string, or blank;
``authority_generation_invalid`` when the authority generation is absent,
null, non-integer, or non-positive; ``stable_deployment_identity_invalid``
when the optional stable service or cluster identity is present but null,
blank, or non-string; and ``secret_material_in_backend_identity`` when any
secret value from the connection material appears in the recorded backend
identity (as a string value, nested under any mapping or list).

It fails closed with ``ValueError`` for a non-mapping declaration, or a
missing, null, non-mapping, or empty connection material or recorded backend
identity, so a complete, secret-safe identity cannot be silently verified
away. No filesystem, network, model, git, or subprocess access occurs.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker
``REQ-CONFIG-019-SHARED-CONTROL-STATE-BACKEND-IDENTITY-GUARD-MISSING``
instead of silently accepting a recorded backend identity that is
under-declared or that leaks secret connection material.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_shared_control_state_backend_identity
# is added. It is a literal, unique string that does not appear anywhere else
# in the repository, so its appearance in a test run is deterministic RED
# evidence.
_GUARD_MISSING = "REQ-CONFIG-019-SHARED-CONTROL-STATE-BACKEND-IDENTITY-GUARD-MISSING"

_MISSING = object()

_SECRET_PASSWORD = "s3cr3t-pw"
_SECRET_TOKEN = "tok-abc-123"
_SERVICE_IDENTITY = "converge-control/cluster-a"


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_shared_control_state_backend_identity
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_shared_control_state_backend_identity


def _connection_material() -> dict[str, str]:
    """One secret-safe connection-material mapping; its values must never be recorded."""
    return {"password": _SECRET_PASSWORD, "token": _SECRET_TOKEN}


def _declaration(**overrides: Any) -> dict[str, Any]:
    """A complete non-secret postgres backend-identity declaration."""
    declaration: dict[str, Any] = {
        "backend_kind": "postgres",
        "logical_database_or_namespace": "converge_control",
        "authority_generation": 3,
        "stable_deployment_identity": _SERVICE_IDENTITY,
        "connection_material": _connection_material(),
        "recorded_backend_identity": {
            "backend_kind": "postgres",
            "logical_database": "converge_control",
            "authority_generation": 3,
            "service_identity": _SERVICE_IDENTITY,
        },
    }
    declaration.update(overrides)
    return declaration


def test_complete_postgres_identity_is_accepted() -> None:
    # A complete non-secret identity distinguishes backend kind, logical
    # database, authority generation, and the stable service/cluster identity:
    # the guard accepts it with no clause.
    assert _guard()(_declaration()) == ()


def test_complete_sqlite_namespace_identity_without_service_identity_is_accepted() -> None:
    # The stable service or cluster identity is optional: a complete sqlite
    # namespace identity recorded without it is accepted with no clause.
    declaration = {
        "backend_kind": "sqlite",
        "logical_database_or_namespace": "converge-control",
        "authority_generation": 1,
        "connection_material": _connection_material(),
        "recorded_backend_identity": {
            "backend_kind": "sqlite",
            "logical_namespace": "converge-control",
            "authority_generation": 1,
        },
    }
    assert _guard()(declaration) == ()


@pytest.mark.parametrize(
    "backend_kind",
    [_MISSING, None, "", "   ", 123],
    ids=["missing", "null", "empty", "blank", "non-string"],
)
def test_undeclared_backend_kind_is_rejected(backend_kind: Any) -> None:
    # Without a declared backend kind the recorded identity cannot
    # distinguish backend kind: the guard reports the undeclared-kind clause.
    declaration = _declaration()
    if backend_kind is _MISSING:
        del declaration["backend_kind"]
    else:
        declaration["backend_kind"] = backend_kind
    assert _guard()(declaration) == ("backend_kind_undeclared",)


@pytest.mark.parametrize(
    "logical",
    [_MISSING, None, "", "   ", 123],
    ids=["missing", "null", "empty", "blank", "non-string"],
)
def test_undeclared_logical_database_or_namespace_is_rejected(logical: Any) -> None:
    # Without a logical database or namespace the recorded identity cannot
    # distinguish the backend instance: the guard reports the undeclared
    # logical-database-or-namespace clause.
    declaration = _declaration()
    if logical is _MISSING:
        del declaration["logical_database_or_namespace"]
    else:
        declaration["logical_database_or_namespace"] = logical
    assert _guard()(declaration) == ("logical_database_or_namespace_undeclared",)


@pytest.mark.parametrize(
    "generation",
    [_MISSING, None, "3", 0, -1, True],
    ids=["missing", "null", "non-integer", "zero", "negative", "boolean"],
)
def test_invalid_authority_generation_is_rejected(generation: Any) -> None:
    # A missing, null, non-integer, or non-positive authority generation
    # cannot order authority generations: the guard reports the invalid
    # authority-generation clause.
    declaration = _declaration()
    if generation is _MISSING:
        del declaration["authority_generation"]
    else:
        declaration["authority_generation"] = generation
    assert _guard()(declaration) == ("authority_generation_invalid",)


@pytest.mark.parametrize(
    "service_identity",
    [None, "", "   ", 123],
    ids=["null", "empty", "blank", "non-string"],
)
def test_invalid_stable_deployment_identity_is_rejected(service_identity: Any) -> None:
    # The stable service or cluster identity is optional, so its absence is
    # accepted (tested above); a present value that is not a non-empty string
    # is malformed: the guard reports the invalid identity clause.
    declaration = _declaration()
    declaration["stable_deployment_identity"] = service_identity
    assert _guard()(declaration) == ("stable_deployment_identity_invalid",)


def test_secret_material_in_recorded_identity_is_reported() -> None:
    # A secret connection value recorded in the backend identity breaks the
    # secret-safe recording contract.
    declaration = _declaration(
        recorded_backend_identity={
            "backend_kind": "postgres",
            "logical_database": "converge_control",
            "authority_generation": 3,
            "password": _SECRET_PASSWORD,
        },
    )
    assert _guard()(declaration) == ("secret_material_in_backend_identity",)


def test_nested_secret_material_in_recorded_identity_is_reported() -> None:
    # Secret material nested under any mapping or list of the recorded
    # identity still appears in the recorded identity.
    declaration = _declaration(
        recorded_backend_identity={
            "backend_kind": "sqlite",
            "logical_namespace": "converge-control",
            "authority_generation": 1,
            "credentials": [{"token": _SECRET_TOKEN}],
        },
    )
    assert _guard()(declaration) == ("secret_material_in_backend_identity",)


def test_dsn_secret_material_in_recorded_identity_is_reported() -> None:
    # A recorded connection string that embeds a secret value still carries
    # secret material in the recorded identity.
    dsn = f"postgres://user:{_SECRET_PASSWORD}@db-host:5432/converge_control"
    declaration = _declaration(recorded_backend_identity={"dsn": dsn})
    assert _guard()(declaration) == ("secret_material_in_backend_identity",)


def test_all_violations_report_in_fixed_clause_order() -> None:
    # Every violated component is reported, in the fixed clause order:
    # backend kind, logical database or namespace, authority generation,
    # stable deployment identity, then secret material in the recorded
    # identity.
    declaration = {
        "backend_kind": None,
        "logical_database_or_namespace": "",
        "authority_generation": 0,
        "stable_deployment_identity": "   ",
        "connection_material": _connection_material(),
        "recorded_backend_identity": {"password": _SECRET_PASSWORD},
    }
    assert _guard()(declaration) == (
        "backend_kind_undeclared",
        "logical_database_or_namespace_undeclared",
        "authority_generation_invalid",
        "stable_deployment_identity_invalid",
        "secret_material_in_backend_identity",
    )


@pytest.mark.parametrize(
    "declaration",
    ["converge-control/cluster-a", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_declaration_fails_closed(declaration: Any) -> None:
    # A declaration that is not a mapping cannot be assessed at all: the
    # guard fails closed with ValueError.
    with pytest.raises(ValueError):
        _guard()(declaration)


@pytest.mark.parametrize(
    "connection_material",
    [_MISSING, None, {}, "s3cr3t-pw"],
    ids=["missing", "null", "empty", "non-mapping"],
)
def test_missing_or_empty_connection_material_fails_closed(connection_material: Any) -> None:
    # Without connection material the guard cannot decide that the recorded
    # identity is secret-safe: it fails closed with ValueError.
    declaration = _declaration()
    if connection_material is _MISSING:
        del declaration["connection_material"]
    else:
        declaration["connection_material"] = connection_material
    with pytest.raises(ValueError):
        _guard()(declaration)


@pytest.mark.parametrize(
    "recorded_identity",
    [_MISSING, None, {}, "postgres:converge_control"],
    ids=["missing", "null", "empty", "non-mapping"],
)
def test_missing_or_empty_recorded_backend_identity_fails_closed(recorded_identity: Any) -> None:
    # Without a recorded backend identity there is nothing to assess as a
    # non-secret identity: the guard fails closed with ValueError.
    declaration = _declaration()
    if recorded_identity is _MISSING:
        del declaration["recorded_backend_identity"]
    else:
        declaration["recorded_backend_identity"] = recorded_identity
    with pytest.raises(ValueError):
        _guard()(declaration)
