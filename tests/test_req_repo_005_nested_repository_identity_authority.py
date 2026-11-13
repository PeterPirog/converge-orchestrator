"""RED lock test for REQ-REPO-005: nested-repository identity authority gate.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): nested Git repositories, submodules,
linked worktrees, and external repository mounts SHALL remain separate repository identities;
editing their internal contents SHALL require explicit project authority for that nested
identity rather than being implied by authority over the primary target repository.

These deterministic offline tests lock the not-yet-existing pure gate
``converge_orchestrator.repository_identity_authority.evaluate_nested_repository_identity_authority``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``REQ-REPO-005 nested identity edit denied without explicit project authority``;
- once the primitive is added, the same tests must pass. It takes a plain mapping describing the
  primary target repository authority, the nested repository boundaries inside the project, and
  the explicit project authority grants for each nested identity. It returns a mapping from each
  nested boundary path to the denial clauses for that boundary in fixed order:

  * ``"primary_target_authority_not_transitive_to_nested_identity"`` -- the controller holds
    authority over the primary target repository, but that authority is not transitive to this
    nested repository identity;
  * ``"nested_identity_explicit_grant_missing_or_mismatched"`` -- no explicit project authority
    grant for this nested identity is present, or the grant is empty, non-matching, or malformed.

A nested boundary whose explicit project authority grant exactly matches its identity class
yields an empty tuple of denial clauses for that path. All inputs are plain values so the gate
stays pure and deterministic in memory. No network, model, provider, subprocess, git command, or
filesystem side effect is exercised in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import cast

import pytest

_MISSING = "REQ-REPO-005 nested identity edit denied without explicit project authority"
_MODULE = "converge_orchestrator.repository_identity_authority"
_NAME = "evaluate_nested_repository_identity_authority"

_NESTED_GIT_REPOSITORY = "nested_git_repository"
_SUBMODULE = "submodule"
_LINKED_WORKTREE = "linked_worktree"
_EXTERNAL_REPOSITORY_MOUNT = "external_repository_mount"

_CLAUSE_PRIMARY_NOT_TRANSITIVE = "primary_target_authority_not_transitive_to_nested_identity"
_CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED = (
    "nested_identity_explicit_grant_missing_or_mismatched"
)

# A pure gate: a plain nested-identity authority mapping -> a mapping from nested boundary path
# to the (possibly empty) tuple of denial clauses for that boundary.
Guard = Callable[[object], Mapping[str, tuple[str, ...]]]


def _guard() -> Guard:
    """Resolve the REQ-REPO-005 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.repository_identity_authority`` does not exist) each test fails with
    the literal ``REQ-REPO-005 nested identity edit denied without explicit project authority``
    marker rather than an opaque import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "REQ-REPO-005 requires a deterministic, offline nested-repository identity authority "
            "gate that fails closed whenever an edit inside a nested Git repository, submodule, "
            "linked worktree, or external repository mount lacks an explicit project authority "
            "grant for that nested identity."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "REQ-REPO-005 requires a deterministic nested-repository identity authority guard "
        "primitive."
    )
    return cast(Guard, guard)


def _state(
    *,
    primary_target_authority: object = None,
    nested_boundaries: Mapping[str, object] | None = None,
    project_authority_grants: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a plain nested-identity authority mapping with no coupling to controller state."""
    return {
        "primary_target_authority": primary_target_authority,
        "nested_boundaries": nested_boundaries if nested_boundaries is not None else {},
        "project_authority_grants": project_authority_grants
        if project_authority_grants is not None
        else {},
    }


def test_nested_git_repository_edit_denied_without_explicit_project_grant() -> None:
    guard = _guard()
    # Authority over the primary target repository is present, but there is no explicit grant
    # for the nested Git repository, so the edit is denied.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_submodule_edit_denied_without_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"modules/lib": _SUBMODULE},
        )
    )
    assert result == {
        "modules/lib": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_linked_worktree_edit_denied_without_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={".converge/worktrees/feature": _LINKED_WORKTREE},
        )
    )
    assert result == {
        ".converge/worktrees/feature": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_external_repository_mount_edit_denied_without_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"mounts/external": _EXTERNAL_REPOSITORY_MOUNT},
        )
    )
    assert result == {
        "mounts/external": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_nested_git_repository_admitted_with_exact_explicit_project_grant() -> None:
    guard = _guard()
    # An explicit project authority grant that exactly matches the nested identity class admits
    # the edit, even when primary target authority is also present.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
            project_authority_grants={"vendor/embedded": _NESTED_GIT_REPOSITORY},
        )
    )
    assert result == {"vendor/embedded": ()}


def test_submodule_admitted_with_exact_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"modules/lib": _SUBMODULE},
            project_authority_grants={"modules/lib": _SUBMODULE},
        )
    )
    assert result == {"modules/lib": ()}


def test_linked_worktree_admitted_with_exact_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={".converge/worktrees/feature": _LINKED_WORKTREE},
            project_authority_grants={
                ".converge/worktrees/feature": _LINKED_WORKTREE,
            },
        )
    )
    assert result == {".converge/worktrees/feature": ()}


def test_external_repository_mount_admitted_with_exact_explicit_project_grant() -> None:
    guard = _guard()
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"mounts/external": _EXTERNAL_REPOSITORY_MOUNT},
            project_authority_grants={"mounts/external": _EXTERNAL_REPOSITORY_MOUNT},
        )
    )
    assert result == {"mounts/external": ()}


def test_absent_project_authority_grant_fails_closed() -> None:
    guard = _guard()
    # The grant mapping exists but does not contain this boundary path.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
            project_authority_grants={"other/path": _NESTED_GIT_REPOSITORY},
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_empty_project_authority_grant_fails_closed() -> None:
    guard = _guard()
    # An empty-string grant is malformed and cannot match the nested identity class.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
            project_authority_grants={"vendor/embedded": ""},
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_non_matching_project_authority_grant_fails_closed() -> None:
    guard = _guard()
    # A grant for a different identity class does not satisfy the exact-match requirement.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
            project_authority_grants={"vendor/embedded": _SUBMODULE},
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_malformed_project_authority_grant_fails_closed() -> None:
    guard = _guard()
    # A non-string grant value is malformed and cannot exactly match the identity class.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={"vendor/embedded": _NESTED_GIT_REPOSITORY},
            project_authority_grants={"vendor/embedded": ["nested_git_repository"]},
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        )
    }


def test_primary_target_authority_over_granted_still_denied_without_explicit_grant() -> None:
    guard = _guard()
    # Even with an over-granted primary target authority token, every nested identity class is
    # denied unless it has its own explicit project authority grant.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={
                "vendor/embedded": _NESTED_GIT_REPOSITORY,
                "modules/lib": _SUBMODULE,
                ".converge/worktrees/feature": _LINKED_WORKTREE,
                "mounts/external": _EXTERNAL_REPOSITORY_MOUNT,
            },
        )
    )
    assert result == {
        "vendor/embedded": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        ),
        "modules/lib": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        ),
        ".converge/worktrees/feature": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        ),
        "mounts/external": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        ),
    }


def test_identical_primary_target_authority_does_not_collapse_nested_identities() -> None:
    guard = _guard()
    # The same primary target authority is offered for two nested identities. Only the boundary
    # with an exact explicit grant is admitted; the other remains a separate denied identity.
    result = guard(
        _state(
            primary_target_authority="project-abc",
            nested_boundaries={
                "vendor/embedded": _NESTED_GIT_REPOSITORY,
                "modules/lib": _SUBMODULE,
            },
            project_authority_grants={"vendor/embedded": _NESTED_GIT_REPOSITORY},
        )
    )
    assert result == {
        "vendor/embedded": (),
        "modules/lib": (
            _CLAUSE_PRIMARY_NOT_TRANSITIVE,
            _CLAUSE_EXPLICIT_GRANT_MISSING_OR_MISMATCHED,
        ),
    }


def test_non_mapping_input_raises_type_error() -> None:
    guard = _guard()
    # The gate is a decision predicate over a plain mapping; non-mapping input must fail closed
    # with TypeError rather than silently returning a false-negative admission result.
    with pytest.raises(TypeError):
        guard("not a mapping")
