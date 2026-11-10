"""RED-phase tests for REQ-REPO-005: separate nested repository identity authority guard.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): nested Git repositories, submodule
checkouts, linked worktrees, and external repository mounts inside a candidate tree SHALL
remain separate repository identities; editing their internal contents SHALL require explicit
project authority rather than being implied by authority over the primary target repository.

These deterministic, offline tests lock the not-yet-existing pure predicate
``converge_orchestrator.repository_identity.evaluate_repository_identity_authority``:

- against the old implementation the module/symbol does not exist, so every test fails with the
  literal marker ``REQ-REPO-005 nested repository identity authority rejected``;
- once the primitive is added, the same tests must pass.  It takes a plain mapping describing the
  identity of the content being mutated (primary repository or one of the four nested identity
  kinds) together with the set of nested identities the project has explicitly been granted
  authority over.  It returns ``None`` when mutation is admitted and the fixed rejection marker
  when it is not.

No network, model, provider, subprocess, git command, or filesystem side effects are exercised
in the tested path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from typing import cast

_REJECTED = "REQ-REPO-005 nested repository identity authority rejected"
_MODULE = "converge_orchestrator.repository_identity"
_NAME = "evaluate_repository_identity_authority"

_KIND_PRIMARY = "primary_repository"
_KIND_NESTED = "nested_repository"
_KIND_SUBMODULE = "submodule_checkout"
_KIND_WORKTREE = "linked_worktree"
_KIND_EXTERNAL = "external_mount"

_NESTED_KINDS = frozenset({_KIND_NESTED, _KIND_SUBMODULE, _KIND_WORKTREE, _KIND_EXTERNAL})

# Pure guard: plain repository-identity state -> None when admitted, or the fixed marker.
Guard = Callable[[Mapping[str, object]], str | None]


def _guard() -> Guard:
    """Resolve the REQ-REPO-005 primitive, failing with the RED marker if it is absent.

    Every test routes through this helper first, so against the old tree (where
    ``converge_orchestrator.repository_identity`` does not exist) each test fails with the
    literal ``REQ-REPO-005 nested repository identity authority rejected`` marker rather than an
    opaque import error.
    """
    try:
        module = importlib.import_module(_MODULE)
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"{_REJECTED}: {_MODULE}.{_NAME} is not defined; "
            "REQ-REPO-005 requires a deterministic, offline repository-identity authority guard "
            "that classifies nested Git repositories, submodule checkouts, linked worktrees, and "
            "external repository mounts as separate identities and rejects mutations inside them "
            "unless an explicit per-identity authority grant is present."
        ) from exc
    guard = getattr(module, _NAME, None)
    assert callable(guard), (
        f"{_REJECTED}: {_MODULE}.{_NAME} is not defined; "
        "REQ-REPO-005 requires a deterministic repository-identity authority guard primitive."
    )
    return cast(Guard, guard)


def _state(
    kind: str,
    identity_id: object = None,
    grants: Iterable[object] = (),
) -> dict[str, object]:
    """Build a plain repository-identity authority state with no coupling to filesystem state."""
    return {
        "target_identity_kind": kind,
        "nested_identity_id": identity_id,
        "authority_grants": frozenset(grants),
    }


def test_primary_repository_mutation_admitted_without_nested_grant() -> None:
    """Authority over the primary repository is sufficient for in-scope primary content."""
    guard = _guard()
    assert guard(_state(_KIND_PRIMARY)) is None


def test_nested_repository_mutation_rejected_without_grant() -> None:
    """A nested Git repository is a separate identity; primary authority is not enough."""
    guard = _guard()
    assert guard(_state(_KIND_NESTED, identity_id="nested/repo")) == _REJECTED


def test_submodule_checkout_mutation_rejected_without_grant() -> None:
    """A submodule checkout (gitdir pointer file) is a separate identity."""
    guard = _guard()
    assert guard(_state(_KIND_SUBMODULE, identity_id="vendor/lib")) == _REJECTED


def test_linked_worktree_pointer_mutation_rejected_without_grant() -> None:
    """Linked worktree metadata points to a separate repository identity."""
    guard = _guard()
    assert guard(_state(_KIND_WORKTREE, identity_id="feature-worktree")) == _REJECTED


def test_external_mount_mutation_rejected_without_grant() -> None:
    """An external repository mount is a separate identity."""
    guard = _guard()
    assert guard(_state(_KIND_EXTERNAL, identity_id="/mnt/external-repo")) == _REJECTED


def test_nested_identity_admitted_with_exact_authority_grant() -> None:
    """A mutation is admitted only when the project holds an explicit grant for that identity."""
    guard = _guard()
    identity = "nested/repo"
    for kind in _NESTED_KINDS:
        assert guard(_state(kind, identity_id=identity, grants={identity})) is None


def test_nested_identity_rejected_with_grant_for_different_identity() -> None:
    """A grant for one nested identity does not authorize another."""
    guard = _guard()
    assert (
        guard(
            _state(
                _KIND_NESTED,
                identity_id="nested/repo",
                grants={"other-nested-repo"},
            )
        )
        == _REJECTED
    )


def test_unknown_identity_kind_is_rejected() -> None:
    """The guard fails closed for an identity kind it does not recognize."""
    guard = _guard()
    assert guard(_state("unknown_identity_kind", identity_id="x")) == _REJECTED
