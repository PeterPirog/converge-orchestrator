"""RED lock test for REQ-REPO-005: deterministic linked-repository authority guard.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): Nested Git repositories, submodules, linked
worktrees, and external repository mounts SHALL remain separate repository identities; editing
their internal contents SHALL require explicit project authority rather than being implied by
authority over the primary target repository.

These deterministic offline tests lock the not-yet-existing pure primitive
``converge_orchestrator.linked_repository_authority.evaluate_linked_repository_authority``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``REQREPO005_LINKED_REPOSITORY_AUTHORITY_MISSING``;
- once the primitive is added, the same tests must pass. It takes a plain mapping describing the
  primary repository identity, the explicitly-authorized linked repository identities, declared
  boundary records, and a list of candidate mutations. It returns the authority violation
  clauses in a fixed order:

  * ``"separate_repository_identity_absorbed_into_primary"`` -- a mutation attributed to the
    primary repository identity falls inside a declared linked-repository boundary, so the
    separate identity would be absorbed into the primary;
  * ``"linked_repository_edit_without_explicit_authority"`` -- an edit inside a declared boundary
    is attributed to the separate repository identity that is not listed in the project's
    explicit authority.

A candidate mutation outside every boundary, or inside a boundary whose identity is explicitly
authorized, yields no clauses. Non-mapping input fails closed with ``TypeError``. Invalid boundary
or mutation records and unsupported boundary kinds return a fail-closed clause. The guard is pure,
offline, deterministic, and independent of input key order; no network, model, provider,
subprocess, git command, or filesystem side effect is exercised in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import cast

import pytest

_MISSING = "REQREPO005_LINKED_REPOSITORY_AUTHORITY_MISSING"
_MODULE = "converge_orchestrator.linked_repository_authority"
_NAME = "evaluate_linked_repository_authority"

_CLAUSE_ABSORBED = "separate_repository_identity_absorbed_into_primary"
_CLAUSE_UNAUTHORIZED = "linked_repository_edit_without_explicit_authority"
_CLAUSE_INVALID_BOUNDARY = "invalid_boundary_record"
_CLAUSE_UNSUPPORTED_KIND = "unsupported_boundary_kind"
_CLAUSE_INVALID_MUTATION = "invalid_mutation_record"

_PRIMARY = "primary"

# All boundary kinds called out by REQ-REPO-005.
_BOUNDARY_KINDS = (
    "nested_git_repository",
    "submodule",
    "linked_worktree",
    "external_mount",
)

# A pure guard: a plain linked-repository authority mapping -> the (possibly empty) iterable of
# violation clauses, in the fixed order absorbed-identity first, then unauthorized edit.
Guard = Callable[[object], Iterable[str]]


def _guard() -> Guard:
    """Resolve the REQ-REPO-005 primitive, failing with the RED marker if it is absent."""
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
            "REQ-REPO-005 requires a deterministic, offline linked-repository authority guard "
            "that keeps nested Git repositories, submodules, linked worktrees, and external "
            "mounts as separate repository identities and fails closed when authority over the "
            "primary repository would otherwise be absorbed or used without explicit project "
            "authority."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_MISSING}: {_MODULE}.{_NAME} is not defined; "
        "REQ-REPO-005 requires a deterministic linked-repository authority predicate."
    )
    return cast(Guard, guard)


def _mutation(
    *, attributed_identity: str = _PRIMARY, content_path: str = "src/main.py"
) -> dict[str, object]:
    """Build a plain mutation record describing who is editing which path."""
    return {
        "attributed_repository_identity": attributed_identity,
        "content_path": content_path,
    }


def _boundary(
    *, kind: str = "nested_git_repository", path: str = "sub", identity: str = "linked"
) -> dict[str, object]:
    """Build a plain boundary record describing a linked-repository enclosure."""
    return {
        "kind": kind,
        "path": path,
        "repository_identity": identity,
    }


def _state(
    mutations: list[dict[str, object]],
    *,
    boundaries: list[dict[str, object]] | None = None,
    explicit_authority: list[str] | None = None,
    primary_repository_identity: str = _PRIMARY,
) -> dict[str, object]:
    """Build a plain linked-repository authority state with no coupling to controller state."""
    return {
        "primary_repository_identity": primary_repository_identity,
        "explicit_project_authority": explicit_authority or [],
        "boundaries": boundaries or [],
        "mutations": mutations,
    }


def test_mutation_outside_all_boundaries_admitted() -> None:
    guard = _guard()
    # A mutation attributed to the primary identity and not inside any linked-repository boundary
    # is admitted by this gate (other gates may still apply).
    assert tuple(guard(_state([_mutation(content_path="src/main.py")]))) == ()


def test_primary_mutation_inside_nested_git_repository_is_absorbed() -> None:
    guard = _guard()
    boundary = _boundary(kind="nested_git_repository", path="sub", identity="nested-repo")
    mutation = _mutation(content_path="sub/file.py")
    assert tuple(guard(_state([mutation], boundaries=[boundary]))) == (_CLAUSE_ABSORBED,)


def test_primary_mutation_inside_submodule_is_absorbed() -> None:
    guard = _guard()
    boundary = _boundary(kind="submodule", path="vendor/lib", identity="vendor-lib")
    mutation = _mutation(content_path="vendor/lib/src/lib.py")
    assert tuple(guard(_state([mutation], boundaries=[boundary]))) == (_CLAUSE_ABSORBED,)


def test_primary_mutation_inside_linked_worktree_is_absorbed() -> None:
    guard = _guard()
    boundary = _boundary(kind="linked_worktree", path="worktrees/feature", identity="feature-wt")
    mutation = _mutation(content_path="worktrees/feature/README.md")
    assert tuple(guard(_state([mutation], boundaries=[boundary]))) == (_CLAUSE_ABSORBED,)


def test_primary_mutation_inside_external_mount_is_absorbed() -> None:
    guard = _guard()
    boundary = _boundary(kind="external_mount", path="mounts/external", identity="external")
    mutation = _mutation(content_path="mounts/external/data.csv")
    assert tuple(guard(_state([mutation], boundaries=[boundary]))) == (_CLAUSE_ABSORBED,)


def test_separate_identity_edit_without_explicit_authority_is_unauthorized() -> None:
    guard = _guard()
    boundary = _boundary(kind="nested_git_repository", path="sub", identity="nested-repo")
    mutation = _mutation(
        attributed_identity="nested-repo", content_path="sub/private.py"
    )
    assert tuple(guard(_state([mutation], boundaries=[boundary]))) == (_CLAUSE_UNAUTHORIZED,)


def test_separate_identity_edit_with_explicit_authority_is_admitted() -> None:
    guard = _guard()
    boundary = _boundary(kind="nested_git_repository", path="sub", identity="nested-repo")
    mutation = _mutation(
        attributed_identity="nested-repo", content_path="sub/private.py"
    )
    assert (
        tuple(guard(_state([mutation], boundaries=[boundary], explicit_authority=["nested-repo"])))
        == ()
    )


def test_deepest_containing_boundary_wins_for_primary_absorption() -> None:
    guard = _guard()
    outer = _boundary(kind="external_mount", path="vendor", identity="vendor-root")
    inner = _boundary(kind="nested_git_repository", path="vendor/pkg", identity="vendor-pkg")
    # The mutation is inside both boundaries; the deepest boundary determines the identity at
    # risk of being absorbed into the primary.
    mutation = _mutation(content_path="vendor/pkg/core.py")
    assert tuple(guard(_state([mutation], boundaries=[outer, inner]))) == (_CLAUSE_ABSORBED,)


def test_deepest_containing_boundary_wins_for_explicit_authority_check() -> None:
    guard = _guard()
    outer = _boundary(kind="external_mount", path="vendor", identity="vendor-root")
    inner = _boundary(kind="nested_git_repository", path="vendor/pkg", identity="vendor-pkg")
    # The outer identity is explicitly authorized, but the edit reaches into the deeper boundary
    # whose identity is not authorized.
    mutation = _mutation(
        attributed_identity="vendor-root", content_path="vendor/pkg/core.py"
    )
    result = tuple(
        guard(
            _state(
                [mutation],
                boundaries=[outer, inner],
                explicit_authority=["vendor-root"],
            )
        )
    )
    assert result == (_CLAUSE_UNAUTHORIZED,)


def test_non_mapping_state_raises_type_error() -> None:
    guard = _guard()
    # A non-mapping state is malformed input and must fail closed with TypeError rather than
    # returning a false-negative admission decision.
    with pytest.raises(TypeError):
        tuple(guard("not a mapping"))


def test_malformed_boundary_record_fails_closed() -> None:
    guard = _guard()
    # A boundary record that lacks a required field cannot be interpreted, so the guard fails
    # closed with the invalid-boundary-record clause.
    malformed_boundary: dict[str, object] = {"path": "sub", "repository_identity": "nested-repo"}
    mutation = _mutation(content_path="sub/file.py")
    assert tuple(guard(_state([mutation], boundaries=[malformed_boundary]))) == (
        _CLAUSE_INVALID_BOUNDARY,
    )


def test_unsupported_boundary_kind_fails_closed() -> None:
    guard = _guard()
    # A boundary kind outside the REQ-REPO-005 set is not a supported linked-repository identity
    # boundary, so the guard fails closed.
    unsupported_boundary = _boundary(kind="git_subtree", path="sub", identity="subtree")
    mutation = _mutation(content_path="sub/file.py")
    assert tuple(guard(_state([mutation], boundaries=[unsupported_boundary]))) == (
        _CLAUSE_UNSUPPORTED_KIND,
    )


def test_malformed_mutation_record_fails_closed() -> None:
    guard = _guard()
    # A mutation record that lacks a required field cannot be attributed, so the guard fails
    # closed with the invalid-mutation-record clause.
    malformed_mutation: dict[str, object] = {"content_path": "src/main.py"}
    assert tuple(guard(_state([malformed_mutation]))) == (_CLAUSE_INVALID_MUTATION,)


def test_both_clauses_returned_in_fixed_order() -> None:
    guard = _guard()
    boundary = _boundary(kind="nested_git_repository", path="sub", identity="nested-repo")
    primary_mutation = _mutation(content_path="sub/file.py")
    separate_mutation = _mutation(
        attributed_identity="nested-repo", content_path="sub/private.py"
    )
    # Both an absorption violation and an unauthorized-edit violation are present; the clauses
    # are returned in the fixed order: absorbed identity first, unauthorized edit second.
    assert tuple(
        guard(_state([primary_mutation, separate_mutation], boundaries=[boundary]))
    ) == (_CLAUSE_ABSORBED, _CLAUSE_UNAUTHORIZED)
