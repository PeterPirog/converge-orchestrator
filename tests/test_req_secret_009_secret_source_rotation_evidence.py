"""REQ-SECRET-009 durable secret-source and rotation evidence guard.

Durable evidence for an external operation must identify the configured secret
source and the non-secret credential-authority identity without carrying a
credential value. When a secret rotation has been performed, the run is
compatible only if a fresh authority observation proves that the resulting
operation identity and permissions are still valid for the pending action.

These tests pin a new pure, offline evaluator in
``converge_orchestrator.config`` that returns identity-only violation clauses
and never reads, echoes, persists, or returns credential values.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

_MISSING_GUARD_MARKER = "REQ-SECRET-009-SECRET-SOURCE-ROTATION-EVIDENCE-GUARD-MISSING"
_SENTINEL_CREDENTIAL_VALUE = "super-secret-credential-value-req-secret-009"

_CONFIGURED_SECRET_SOURCE = "vault://production/api-keys"
_CREDENTIAL_AUTHORITY_IDENTITY = "iam://role/converge-external-operator"
_PENDING_OPERATION_IDENTITY = _CREDENTIAL_AUTHORITY_IDENTITY
_PENDING_PERMISSIONS = ["read:secrets", "invoke:external"]


def _guard() -> Any:
    """Return the REQ-SECRET-009 evaluator, failing with the RED marker if absent."""
    try:
        from converge_orchestrator.config import evaluate_secret_source_rotation_evidence
    except ImportError as exc:
        raise AssertionError(
            f"{_MISSING_GUARD_MARKER}: "
            "REQ-SECRET-009 secret-source/rotation evidence guard is not exported "
            "from converge_orchestrator.config"
        ) from exc
    return evaluate_secret_source_rotation_evidence


def _state(
    *,
    configured_secret_source_identity: str | None = _CONFIGURED_SECRET_SOURCE,
    credential_authority_identity: str | None = _CREDENTIAL_AUTHORITY_IDENTITY,
    durable_evidence_records: list[dict[str, Any]] | None = None,
    rotation_performed: bool | None = False,
    fresh_authority_observation: dict[str, Any] | None = None,
    pending_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal external-operation run-state for the guard."""
    records = durable_evidence_records
    if records is None:
        records = [
            {
                "secret_source_identity": _CONFIGURED_SECRET_SOURCE,
                "credential_authority_identity": _CREDENTIAL_AUTHORITY_IDENTITY,
            },
        ]
    if pending_action is None:
        pending_action = {
            "operation_identity": _PENDING_OPERATION_IDENTITY,
            "permissions": list(_PENDING_PERMISSIONS),
        }
    state: dict[str, Any] = {
        "configured_secret_source_identity": configured_secret_source_identity,
        "credential_authority_identity": credential_authority_identity,
        "durable_evidence_records": records,
        "rotation_performed": rotation_performed,
        "pending_action": pending_action,
    }
    if fresh_authority_observation is not None:
        state["fresh_authority_observation"] = fresh_authority_observation
    return state


def test_durable_evidence_identifies_source_and_authority() -> None:
    """Complete identity-only evidence with no rotation passes without clauses."""
    guard = _guard()
    assert guard(_state()) == ()


def test_durable_evidence_omits_configured_secret_source_identity() -> None:
    """A record that does not name the configured secret source emits a clause."""
    guard = _guard()
    state = _state(
        durable_evidence_records=[
            {"credential_authority_identity": _CREDENTIAL_AUTHORITY_IDENTITY},
        ],
    )
    assert guard(state) == ("durable_evidence_omits_configured_secret_source_identity",)


def test_durable_evidence_omits_credential_authority_identity() -> None:
    """A record that does not name the credential authority emits a clause."""
    guard = _guard()
    state = _state(
        durable_evidence_records=[
            {"secret_source_identity": _CONFIGURED_SECRET_SOURCE},
        ],
    )
    assert guard(state) == ("durable_evidence_omits_credential_authority_identity",)


def test_durable_evidence_carries_credential_value_field() -> None:
    """A record carrying a credential value field emits a clause that omits the value."""
    guard = _guard()
    state = _state(
        durable_evidence_records=[
            {
                "secret_source_identity": _CONFIGURED_SECRET_SOURCE,
                "credential_authority_identity": _CREDENTIAL_AUTHORITY_IDENTITY,
                "credential_value": _SENTINEL_CREDENTIAL_VALUE,
            },
        ],
    )
    clauses = guard(state)
    assert clauses == ("durable_evidence_carries_credential_value",)
    assert _SENTINEL_CREDENTIAL_VALUE not in " ".join(clauses)


def test_secret_rotation_without_fresh_authority_observation() -> None:
    """A performed rotation with no fresh observation is incompatible."""
    guard = _guard()
    state = _state(rotation_performed=True)
    assert guard(state) == ("secret_rotation_missing_fresh_authority_observation",)


def test_secret_rotation_with_valid_fresh_observation() -> None:
    """A rotation followed by a matching fresh observation passes."""
    guard = _guard()
    state = _state(
        rotation_performed=True,
        fresh_authority_observation={
            "operation_identity": _PENDING_OPERATION_IDENTITY,
            "permissions": list(_PENDING_PERMISSIONS),
        },
    )
    assert guard(state) == ()


def test_secret_rotation_fresh_identity_mismatch() -> None:
    """A fresh observation naming a different operation identity is incompatible."""
    guard = _guard()
    state = _state(
        rotation_performed=True,
        fresh_authority_observation={
            "operation_identity": "iam://role/other-operator",
            "permissions": list(_PENDING_PERMISSIONS),
        },
    )
    assert guard(state) == ("secret_rotation_fresh_identity_mismatch",)


def test_secret_rotation_fresh_permissions_mismatch() -> None:
    """A fresh observation with different permissions is incompatible."""
    guard = _guard()
    state = _state(
        rotation_performed=True,
        fresh_authority_observation={
            "operation_identity": _PENDING_OPERATION_IDENTITY,
            "permissions": ["read:secrets"],
        },
    )
    assert guard(state) == ("secret_rotation_fresh_permissions_mismatch",)


def test_guard_fails_closed_on_non_mapping_state() -> None:
    """The evaluator is fail-closed: non-mapping state raises ValueError."""
    guard = _guard()
    with pytest.raises(ValueError):
        guard(None)


@pytest.mark.parametrize(
    "drop_key",
    [
        "configured_secret_source_identity",
        "credential_authority_identity",
        "durable_evidence_records",
        "rotation_performed",
        "pending_action",
    ],
)
def test_guard_fails_closed_on_missing_required_declaration(drop_key: str) -> None:
    """Missing required declarations raise ValueError rather than silently pass."""
    guard = _guard()
    state = _state()
    del state[drop_key]
    with pytest.raises(ValueError):
        guard(state)


def test_guard_fails_closed_on_malformed_evidence_record() -> None:
    """Malformed durable-evidence records raise ValueError."""
    guard = _guard()
    state = _state(durable_evidence_records=cast(list[dict[str, Any]], ["not-a-mapping"]))
    with pytest.raises(ValueError):
        guard(state)
