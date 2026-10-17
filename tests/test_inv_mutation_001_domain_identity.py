"""RED lock test for INV-MUTATION-001: canonical repository mutation domain identity.

INV-MUTATION-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L165 [4. Global invariants]):
At most one authoritative mutation lease SHALL exist for one repository mutation domain
identity at a time, regardless of how many project IDs, controller processes, local clones,
worktrees, or state stores refer to that domain.

The invariant is defined over a "repository mutation domain identity" that does not yet
exist in the codebase (only the clone-scoped ``workspace_id`` and the state-store-scoped
``state_store_id`` are present). These deterministic offline tests therefore lock the
not-yet-existing primitive ``converge_orchestrator.workspace_identity.mutation_domain_id``:

- against the old implementation the module has no such symbol, so every test fails with
  the literal marker ``MUTATION001_REPOSITORY_MUTATION_DOMAIN_IDENTITY_MISSING``;
- once the primitive is added, the same tests must pass: the identity is equal for
  independent clones and linked worktrees of one repository, distinct for unrelated
  repositories, and stable/independent of any state store.

All fixtures use local git only (no network, model, provider, or fetch) and run on both
Windows and POSIX ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import converge_orchestrator.workspace_identity as workspace_identity

_MISSING = "MUTATION001_REPOSITORY_MUTATION_DOMAIN_IDENTITY_MISSING"


def _run_git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    _run_git("init", "-b", "main", str(path))
    _run_git("config", "user.name", "ci", cwd=path)
    _run_git("config", "user.email", "ci@example.invalid", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=path)
    _run_git("commit", "-m", "seed", cwd=path)
    return path


def _clone(origin: Path, target: Path) -> Path:
    _run_git("clone", str(origin), str(target))
    return target


def _worktree_add(base: Path, target: Path) -> Path:
    _run_git("worktree", "add", str(target), cwd=base)
    return target


def _mutation_domain_id(repo: Path) -> str:
    """Resolve the INV-MUTATION-001 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(workspace_identity, "mutation_domain_id", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.mutation_domain_id(repo) is not defined; "
        "INV-MUTATION-001 requires one canonical repository mutation domain identity "
        "shared by independent clones and linked worktrees of a repository."
    )
    domain_id: Callable[[Path], str] = cast("Callable[[Path], str]", primitive)
    return domain_id(repo)


def test_domain_identity_equal_across_independent_clones(tmp_path: Path) -> None:
    origin = _git_repo(tmp_path / "origin")
    clone_a = _clone(origin, tmp_path / "clone_a")
    clone_b = _clone(origin, tmp_path / "clone_b")

    assert _mutation_domain_id(clone_a) == _mutation_domain_id(clone_b)


def test_domain_identity_equal_across_linked_worktrees(tmp_path: Path) -> None:
    origin = _git_repo(tmp_path / "origin")
    base = _clone(origin, tmp_path / "base")
    sibling = _worktree_add(base, tmp_path / "sibling")

    assert _mutation_domain_id(base) == _mutation_domain_id(sibling)


def test_domain_identity_distinct_across_unrelated_repositories(tmp_path: Path) -> None:
    origin_a = _git_repo(tmp_path / "origin_a")
    origin_b = _git_repo(tmp_path / "origin_b")
    clone_a = _clone(origin_a, tmp_path / "clone_a")
    clone_b = _clone(origin_b, tmp_path / "clone_b")

    assert _mutation_domain_id(clone_a) != _mutation_domain_id(clone_b)


def test_domain_identity_is_stable_and_state_store_independent(tmp_path: Path) -> None:
    origin = _git_repo(tmp_path / "origin")
    clone = _clone(origin, tmp_path / "clone")

    first = _mutation_domain_id(clone)
    assert _mutation_domain_id(clone) == first
    assert isinstance(first, str) and first.strip() != ""
