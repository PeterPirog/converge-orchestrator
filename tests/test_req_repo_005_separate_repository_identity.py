"""RED lock test for REQ-REPO-005: separate nested repository identity and edit authority.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): Nested Git repositories, submodules,
linked worktrees, and external repository mounts SHALL remain separate repository
identities; editing their internal contents SHALL require explicit project authority
rather than being implied by authority over the primary target repository.

These deterministic offline tests lock the not-yet-existing primitives in
``converge_orchestrator.workspace_identity``:

- ``resolve_repository_identity(path)`` returns the deterministic Git repository
  identity that owns ``path``.
- ``assert_nested_repository_authority(project, repo_root, path)`` rejects editing
  ``path`` unless it belongs to the same repository identity as ``repo_root`` or the
  project explicitly names the separate nested identity via ``allowed_paths``.

Against the old implementation the module has neither symbol, so every test fails with
the literal marker ``REQREPO005_SEPARATE_REPOSITORY_IDENTITY_AUTHORITY_MISSING``.
Once the primitives are added, the same tests must pass and must not regress
existing workspace affinity or mutation-domain semantics.

All fixtures use local git only (no network, model, provider, or fetch) and run on
both Windows and POSIX ``tmp_path``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import converge_orchestrator.workspace_identity as workspace_identity
from converge_orchestrator.workspace_identity import WorkspaceAffinityError

_MISSING = "REQREPO005_SEPARATE_REPOSITORY_IDENTITY_AUTHORITY_MISSING"


def _run_git(*args: str, cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}) in {cwd}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )


def _git_repo(path: Path) -> Path:
    """Create a minimal git repository with an initial commit at ``path``."""
    path.mkdir(parents=True, exist_ok=False)
    _run_git("init", "-b", "main", str(path))
    _run_git("config", "user.name", "ci", cwd=path)
    _run_git("config", "user.email", "ci@example.invalid", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git("add", ".", cwd=path)
    _run_git("commit", "-m", "seed", cwd=path)
    return path


def _worktree_add(base: Path, target: Path) -> Path:
    """Add a linked worktree of the repository at ``base`` at ``target``."""
    _run_git("worktree", "add", str(target), cwd=base)
    return target


def _mount_external(repo_root: Path, target: Path, external: Path) -> Path:
    """Simulate an external repository mount inside ``repo_root``.

    Uses a directory junction on Windows and a symlink on POSIX. The resulting path
    looks like a child of ``repo_root`` but is backed by the separate repository at
    ``external``, so its Git identity must remain that of ``external``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(external)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        target.symlink_to(external, target_is_directory=True)
    return target


def _resolve_repository_identity(path: Path) -> str:
    """Resolve the REQ-REPO-005 identity primitive, failing with the RED marker if absent."""
    primitive = getattr(workspace_identity, "resolve_repository_identity", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.resolve_repository_identity(path) is not defined; "
        "REQ-REPO-005 requires a deterministic offline primitive that resolves the Git "
        "repository identity owning a path."
    )
    identity: Callable[[Path], str] = cast("Callable[[Path], str]", primitive)
    return identity(path)


def _assert_nested_repository_authority(
    project: dict, repo_root: Path, path: Path
) -> None:
    """Resolve the REQ-REPO-005 authority primitive, failing with the RED marker if absent."""
    primitive = getattr(workspace_identity, "assert_nested_repository_authority", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.assert_nested_repository_authority(...) is not defined; "
        "REQ-REPO-005 requires an explicit edit-authority predicate that rejects editing "
        "contents of a separate repository identity unless the project explicitly names "
        "the nested root in allowed_paths."
    )
    authority: Callable[[dict, Path, Path], None] = cast(
        "Callable[[dict, Path, Path], None]", primitive
    )
    return authority(project, repo_root, path)


def test_same_identity_for_primary_repository_and_its_linked_worktree(
    tmp_path: Path,
) -> None:
    """Candidate worktrees of the primary repository share one identity."""
    primary = _git_repo(tmp_path / "primary")
    sibling = _worktree_add(primary, tmp_path / "sibling")

    assert _resolve_repository_identity(sibling) == _resolve_repository_identity(primary)


def test_nested_repository_has_separate_identity(tmp_path: Path) -> None:
    """A git repository nested inside the primary working tree is a foreign identity."""
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")

    nested_identity = _resolve_repository_identity(nested / "seed.txt")
    primary_identity = _resolve_repository_identity(primary)

    assert nested_identity != primary_identity


def test_submodule_has_separate_identity(tmp_path: Path) -> None:
    """A git submodule remains a separate repository identity."""
    primary = _git_repo(tmp_path / "primary")
    external = _git_repo(tmp_path / "external")
    submodule_path = primary / "vendor" / "sub"
    _run_git(
        "-c", "protocol.file.allow=always", "submodule", "add",
        str(external), submodule_path.relative_to(primary).as_posix(), cwd=primary,
    )
    _run_git("commit", "-m", "add submodule", cwd=primary)

    submodule_identity = _resolve_repository_identity(submodule_path / "seed.txt")
    primary_identity = _resolve_repository_identity(primary)

    assert submodule_identity != primary_identity


def test_foreign_linked_worktree_has_separate_identity(tmp_path: Path) -> None:
    """A linked worktree of a foreign repository mounted inside primary is foreign."""
    primary = _git_repo(tmp_path / "primary")
    foreign = _git_repo(tmp_path / "foreign")
    mount = primary / "vendor" / "foreign"
    _run_git("worktree", "add", str(mount), cwd=foreign)

    mount_identity = _resolve_repository_identity(mount / "seed.txt")
    primary_identity = _resolve_repository_identity(primary)

    assert mount_identity != primary_identity


def test_external_mount_has_separate_identity(tmp_path: Path) -> None:
    """An externally mounted repository inside primary keeps its own identity."""
    primary = _git_repo(tmp_path / "primary")
    external = _git_repo(tmp_path / "external")
    mount = _mount_external(primary, primary / "vendor" / "external", external)

    mount_identity = _resolve_repository_identity(mount / "seed.txt")
    primary_identity = _resolve_repository_identity(primary)
    external_identity = _resolve_repository_identity(external)

    assert mount_identity != primary_identity
    assert mount_identity == external_identity


def test_explicit_allowed_nested_edit_is_authorized(tmp_path: Path) -> None:
    """A project whose allowed_paths names the nested root may edit inside it."""
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    project = {"id": "proj", "allowed_paths": ["vendor/nested/**"]}

    _assert_nested_repository_authority(project, primary, nested / "seed.txt")


def test_unauthorized_nested_edit_is_rejected(tmp_path: Path) -> None:
    """A project with only primary-repo authority may not edit a nested repository."""
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    project = {"id": "proj", "allowed_paths": ["src/**"]}

    with pytest.raises(WorkspaceAffinityError):
        _assert_nested_repository_authority(project, primary, nested / "seed.txt")


def test_same_identity_path_is_always_authorized(tmp_path: Path) -> None:
    """Authority over the primary identity is sufficient for paths inside that identity."""
    primary = _git_repo(tmp_path / "primary")
    project = {"id": "proj", "allowed_paths": ["src/**"]}

    _assert_nested_repository_authority(project, primary, primary / "seed.txt")
