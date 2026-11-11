"""RED lock test for REQ-REPO-005: nested repository mutation-boundary guard.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): Nested Git repositories, submodules,
linked worktrees, and external repository mounts SHALL remain separate repository identities;
editing their internal contents SHALL require explicit project authority rather than being
implied by authority over the primary target repository.

These deterministic offline tests lock the not-yet-existing pure primitive
``converge_orchestrator.workspace_identity.evaluate_nested_repository_boundary``:

- against the old implementation the symbol does not exist, so every test fails with the
  literal marker ``REQ-REPO-005_NESTED_REPOSITORY_BOUNDARY_GUARD_MISSING``;
- once the primitive is added, the same tests must pass. It takes the primary target
  repository, an iterable of change paths, and an optional set of explicitly declared
  repository mutation-domain authorities. It returns the violated clauses, and fails closed
  when a change path lies inside a separate Git identity (nested repository, submodule,
  linked worktree of another repository, or external mount) that is not covered by an
  explicit authority. Ordinary paths inside the primary repository report no clauses.

All fixtures use local git only (no network, model, provider, or fetch) and run on both
Windows and POSIX ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import converge_orchestrator.workspace_identity as workspace_identity

_MISSING = "REQ-REPO-005_NESTED_REPOSITORY_BOUNDARY_GUARD_MISSING"
_CLAUSE = "nested_repository_boundary_violated"


def _run_git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path: Path) -> Path:
    """Create a fresh git repository with an initial commit."""
    path.mkdir(parents=True, exist_ok=False)
    _run_git("init", "-b", "main", str(path))
    _run_git("config", "user.name", "ci", cwd=path)
    _run_git("config", "user.email", "ci@example.invalid", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=path)
    _run_git("commit", "-m", "seed", cwd=path)
    return path


def _dir_alias(link: Path, target: Path) -> None:
    """Create a directory junction on Windows or a symlink elsewhere."""
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


def _guard(repo: Path, changes: list[Path], declared: set[str] | None = None) -> tuple[str, ...]:
    """Resolve the REQ-REPO-005 primitive, failing with the RED marker if it is absent."""
    primitive = getattr(workspace_identity, "evaluate_nested_repository_boundary", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.evaluate_nested_repository_boundary is not defined; "
        "REQ-REPO-005 requires a deterministic, offline nested-repository mutation-boundary guard."
    )
    return cast("tuple[str, ...]", primitive(repo, changes, declared))


def test_ordinary_paths_report_no_clauses(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")

    changes = [primary / "src" / "main.py", primary / "README.md"]
    assert _guard(primary, changes) == ()


def test_nested_repository_reports_violation(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    (nested / "file.txt").write_text("edit\n", encoding="utf-8")

    changes = [nested / "file.txt"]
    assert _guard(primary, changes) == (_CLAUSE,)


def test_submodule_reports_violation(tmp_path: Path) -> None:
    origin = _git_repo(tmp_path / "submodule_origin")
    primary = _git_repo(tmp_path / "primary")
    _run_git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", str(origin), "vendor/submod",
        cwd=primary,
    )
    _run_git("commit", "-m", "add submodule", cwd=primary)
    submod = primary / "vendor" / "submod"
    (submod / "file.txt").write_text("edit\n", encoding="utf-8")

    changes = [submod / "file.txt"]
    assert _guard(primary, changes) == (_CLAUSE,)


def test_linked_worktree_of_another_repository_reports_violation(tmp_path: Path) -> None:
    other = _git_repo(tmp_path / "other")
    primary = _git_repo(tmp_path / "primary")
    other_worktree = tmp_path / "other_worktree"
    _run_git("worktree", "add", "-b", "feature", str(other_worktree), cwd=other)
    (other_worktree / "file.txt").write_text("edit\n", encoding="utf-8")

    # Mount the other repository's linked worktree inside the primary worktree.
    mount = primary / "vendor" / "linked"
    mount.parent.mkdir(parents=True, exist_ok=True)
    _dir_alias(mount, other_worktree)

    try:
        changes = [mount / "file.txt"]
        assert _guard(primary, changes) == (_CLAUSE,)
    finally:
        if sys.platform == "win32":
            mount.rmdir()
        else:
            os.unlink(mount)


def test_external_mount_reports_violation(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    external = _git_repo(tmp_path / "external")
    (external / "file.txt").write_text("edit\n", encoding="utf-8")

    mount = primary / "vendor" / "external"
    mount.parent.mkdir(parents=True, exist_ok=True)
    _dir_alias(mount, external)

    try:
        changes = [mount / "file.txt"]
        assert _guard(primary, changes) == (_CLAUSE,)
    finally:
        if sys.platform == "win32":
            mount.rmdir()
        else:
            os.unlink(mount)


def test_explicit_declared_authority_reports_no_clause(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    (nested / "file.txt").write_text("edit\n", encoding="utf-8")

    nested_domain = workspace_identity.mutation_domain_id(nested)
    changes = [nested / "file.txt"]
    assert _guard(primary, changes, declared={nested_domain}) == ()
