"""REQ-SECRET-002: external secrets-file boundary isolation in preflight.

REQ-SECRET-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L222
[6. Project configuration and secret sources]): an external secrets file SHALL
reside outside the target repository, candidate worktrees, generated runtime
directory, and evidence tree, and preflight SHALL reject path aliasing,
traversal, symlink, junction, mount, or effective-permission conditions that
violate the configured secret boundary.

These offline, deterministic tests pin that contract against the pure,
fail-closed preflight guard
``converge_orchestrator.config.evaluate_external_secrets_file_boundary`` over a
run-state mapping carrying the declared external secrets-file path, its
resolved real path, alias-kind observations, the declared effective readers, the
configured secret-boundary allowed readers, and the boundary roots (target
repository, candidate worktrees, generated runtime directory, and evidence
tree).

The guard accepts (returns an empty tuple) a declaration whose declared and
resolved real paths stay outside every boundary root, with no traversal, no
alias/link/mount observation, and effective readers that do not exceed the
configured secret-boundary allowed readers.

The guard reports the fixed clauses, in fixed order:

* ``external_secrets_file_inside_target_repository``
* ``external_secrets_file_inside_candidate_worktree``
* ``external_secrets_file_inside_generated_runtime_directory``
* ``external_secrets_file_inside_evidence_tree``

when either the declared or the resolved real path resolves inside the
corresponding root; ``external_secrets_file_path_traversal`` when the declared
path carries a ``..`` segment; ``external_secrets_file_path_aliased`` when the
resolved real path differs from the declared path or an alias kind is symlink,
junction, or mount; and
``external_secrets_file_effective_permission_exceeds_boundary`` when the
declared effective readers are not a subset of the configured secret-boundary
allowed readers.

It defers to REQ-SECRET-001 by returning an empty tuple when the external
secrets-file declaration is absent or null, and fails closed with ``ValueError``
for a blank, non-string, relative, or malformed path, a missing or non-explicit
boundary root, an unknown alias-kind token, or a malformed reader identity.

The guard does not read, echo, persist, or compare secret values; it assesses
only path and boundary facts. No filesystem, network, model, git, or subprocess
access occurs.

The guard does not exist yet, so every evaluation below fails with the literal
TDD RED evidence marker
``REQ-SECRET-002-EXTERNAL-SECRETS-FILE-BOUNDARY-GUARD-MISSING`` instead of
silently accepting an external secrets-file declaration that sits inside a
boundary root, aliases the path, or exceeds the configured secret boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by every
# failure below until the pure guard
# converge_orchestrator.config.evaluate_external_secrets_file_boundary is added.
# It is a literal, unique string that does not appear anywhere else in the
# repository, so its appearance in a test run is deterministic RED evidence.
_GUARD_MISSING = "REQ-SECRET-002-EXTERNAL-SECRETS-FILE-BOUNDARY-GUARD-MISSING"

# Boundary roots, kept disjoint so an individual clause test triggers exactly one
# of the inside clauses under the guard's offline path containment check.
_TARGET_REPOSITORY = "/repo"
_CANDIDATE_WORKTREE = "/worktrees/wt1"
_GENERATED_RUNTIME = "/runtime"
_EVIDENCE_TREE = "/evidence"

# A non-blank absolute external secrets-file path that stays outside every
# boundary root above (platform-independent under the guard's offline check).
_SECRETS_FILE = "/secrets/external"
# The configured secret-boundary allowed readers (the declared effective readers
# that stay within the boundary).
_ALLOWED_READERS = ["svc-orchestrator"]


def _guard():
    """Return the guard, failing closed with the literal RED marker when absent."""
    try:
        from converge_orchestrator.config import evaluate_external_secrets_file_boundary
    except ImportError as exc:
        raise AssertionError(_GUARD_MISSING) from exc
    return evaluate_external_secrets_file_boundary


def _state(**overrides: Any) -> dict[str, Any]:
    """A run-state whose external secrets-file stays outside every boundary root."""
    state: dict[str, Any] = {
        "external_secrets_file": _SECRETS_FILE,
        "external_secrets_file_resolved": _SECRETS_FILE,
        "external_secrets_file_alias_kinds": [],
        "external_secrets_file_effective_readers": list(_ALLOWED_READERS),
        "secret_boundary_allowed_readers": list(_ALLOWED_READERS),
        "target_repository_path": _TARGET_REPOSITORY,
        "candidate_worktree_paths": [_CANDIDATE_WORKTREE],
        "generated_runtime_directory": _GENERATED_RUNTIME,
        "evidence_tree_path": _EVIDENCE_TREE,
    }
    state.update(overrides)
    return state


def test_external_secrets_file_outside_all_roots_is_accepted() -> None:
    # The declared and resolved paths stay outside every boundary root, with no
    # traversal, no alias/link/mount observation, and effective readers within the
    # configured secret boundary: the guard is satisfied and reports no clause.
    assert _guard()(_state()) == ()


def test_inside_target_repository_reports_clause() -> None:
    # The declared and resolved paths resolve inside the target repository: the
    # guard reports the target-repository clause and no other.
    state = _state(
        external_secrets_file=f"{_TARGET_REPOSITORY}/secrets",
        external_secrets_file_resolved=f"{_TARGET_REPOSITORY}/secrets",
    )
    assert _guard()(state) == ("external_secrets_file_inside_target_repository",)


def test_inside_candidate_worktree_reports_clause() -> None:
    # The declared and resolved paths resolve inside a candidate worktree: the
    # guard reports the candidate-worktree clause and no other.
    state = _state(
        external_secrets_file=f"{_CANDIDATE_WORKTREE}/secrets",
        external_secrets_file_resolved=f"{_CANDIDATE_WORKTREE}/secrets",
    )
    assert _guard()(state) == ("external_secrets_file_inside_candidate_worktree",)


def test_inside_generated_runtime_directory_reports_clause() -> None:
    # The declared and resolved paths resolve inside the generated runtime
    # directory: the guard reports the generated-runtime clause and no other.
    state = _state(
        external_secrets_file=f"{_GENERATED_RUNTIME}/secrets",
        external_secrets_file_resolved=f"{_GENERATED_RUNTIME}/secrets",
    )
    assert _guard()(state) == ("external_secrets_file_inside_generated_runtime_directory",)


def test_inside_evidence_tree_reports_clause() -> None:
    # The declared and resolved paths resolve inside the evidence tree: the guard
    # reports the evidence-tree clause and no other.
    state = _state(
        external_secrets_file=f"{_EVIDENCE_TREE}/secrets",
        external_secrets_file_resolved=f"{_EVIDENCE_TREE}/secrets",
    )
    assert _guard()(state) == ("external_secrets_file_inside_evidence_tree",)


def test_inside_all_roots_reports_clauses_in_fixed_order() -> None:
    # The declared and resolved paths resolve inside every boundary root: the
    # guard reports the four inside clauses in their fixed order (target
    # repository, candidate worktree, generated runtime directory, evidence tree).
    state = _state(
        target_repository_path="/root",
        candidate_worktree_paths=["/root/wt"],
        generated_runtime_directory="/root/wt/runtime",
        evidence_tree_path="/root/wt/runtime/evidence",
        external_secrets_file="/root/wt/runtime/evidence/secrets",
        external_secrets_file_resolved="/root/wt/runtime/evidence/secrets",
    )
    assert _guard()(state) == (
        "external_secrets_file_inside_target_repository",
        "external_secrets_file_inside_candidate_worktree",
        "external_secrets_file_inside_generated_runtime_directory",
        "external_secrets_file_inside_evidence_tree",
    )


def test_declared_path_traversal_reports_clause() -> None:
    # The declared path carries a ``..`` segment: the guard reports the traversal
    # clause and no other (the resolved path stays outside every boundary root).
    state = _state(
        external_secrets_file="/secrets/../secrets/external",
        external_secrets_file_resolved=_SECRETS_FILE,
    )
    assert _guard()(state) == ("external_secrets_file_path_traversal",)


def test_resolved_path_differs_from_declared_reports_aliased() -> None:
    # The resolved real path differs from the declared path: the guard reports the
    # aliased clause and no other (both paths stay outside every boundary root).
    state = _state(
        external_secrets_file="/secrets/external",
        external_secrets_file_resolved="/secrets/real",
    )
    assert _guard()(state) == ("external_secrets_file_path_aliased",)


def test_alias_kind_symlink_reports_aliased() -> None:
    # An alias-kind observation of symlink (or junction, or mount) is a path
    # aliasing condition: the guard reports the aliased clause and no other.
    state = _state(external_secrets_file_alias_kinds=["symlink"])
    assert _guard()(state) == ("external_secrets_file_path_aliased",)


def test_effective_readers_exceed_boundary_reports_clause() -> None:
    # The declared effective readers include a reader outside the configured
    # secret-boundary allowed readers: the guard reports the effective-permission
    # clause and no other (the path stays outside every boundary root).
    state = _state(
        external_secrets_file_effective_readers=["svc-orchestrator", "svc-external"],
    )
    assert _guard()(state) == ("external_secrets_file_effective_permission_exceeds_boundary",)


def test_absent_external_secrets_file_defers_to_req_secret_001() -> None:
    # The external secrets-file declaration is absent: the guard defers to
    # REQ-SECRET-001 and returns an empty tuple without assessing the boundary.
    state = _state()
    del state["external_secrets_file"]
    assert _guard()(state) == ()


def test_null_external_secrets_file_defers_to_req_secret_001() -> None:
    # The external secrets-file declaration is null: the guard defers to
    # REQ-SECRET-001 and returns an empty tuple without assessing the boundary.
    assert _guard()(_state(external_secrets_file=None)) == ()


def test_blank_external_secrets_file_fails_closed() -> None:
    # A blank external secrets-file path is not a usable declaration: the guard
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(external_secrets_file="   "))


def test_non_string_external_secrets_file_fails_closed() -> None:
    # A non-string external secrets-file path is malformed: the guard fails
    # closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(external_secrets_file=123))


def test_relative_external_secrets_file_fails_closed() -> None:
    # A relative external secrets-file path is not a first-class absolute path:
    # the guard fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(_state(external_secrets_file="secrets/external"))


@pytest.mark.parametrize(
    "root_key",
    [
        "target_repository_path",
        "candidate_worktree_paths",
        "generated_runtime_directory",
        "evidence_tree_path",
    ],
    ids=["target_repository", "candidate_worktree", "generated_runtime", "evidence_tree"],
)
def test_missing_boundary_root_fails_closed(root_key: str) -> None:
    # A missing boundary root is a fail-closed condition: the guard fails closed
    # with ValueError rather than silently accepting the declaration without the
    # full configured secret boundary.
    state = _state()
    del state[root_key]
    with pytest.raises(ValueError):
        _guard()(state)


def test_unknown_alias_kind_fails_closed() -> None:
    # An unknown alias-kind token is a fail-closed condition: the guard fails
    # closed with ValueError rather than silently accepting an undeclared
    # aliasing observation.
    with pytest.raises(ValueError):
        _guard()(_state(external_secrets_file_alias_kinds=["hardlink"]))


def test_malformed_reader_identity_fails_closed() -> None:
    # A malformed (non-string) reader identity is a fail-closed condition: the
    # guard fails closed with ValueError rather than comparing an ill-formed
    # reader identity against the configured secret boundary.
    with pytest.raises(ValueError):
        _guard()(_state(external_secrets_file_effective_readers=["svc-orchestrator", 123]))


@pytest.mark.parametrize(
    "state",
    ["converge", 123, [], None],
    ids=["string", "number", "list", "null"],
)
def test_non_mapping_state_fails_closed(state: Any) -> None:
    # A run-state that is not a mapping cannot be assessed at all: the guard
    # fails closed with ValueError rather than silently passing.
    with pytest.raises(ValueError):
        _guard()(state)
