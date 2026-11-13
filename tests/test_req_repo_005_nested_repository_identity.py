"""RED lock test for REQ-REPO-005: separate nested repository identities and explicit authority.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): Nested Git repositories, submodules, linked
worktrees, and external repository mounts SHALL remain separate repository identities; editing
their internal contents SHALL require explicit project authority rather than being implied by
authority over the primary target repository.

The required primitives ``resolve_separate_repository_identity`` and
``assert_repository_identity_authority`` do not yet exist in
``converge_orchestrator.workspace_identity``. These deterministic offline tests therefore lock
the not-yet-existing primitives:

- against the old implementation the module has no such symbols, so every test fails with
  the literal marker ``REQREPO005_SEPARATE_REPOSITORY_IDENTITY_PRIMITIVE_MISSING``;
- once the primitives are added, the same tests must pass: paths inside a nested repo,
  submodule, foreign linked worktree, or external mount under the primary tree resolve to
  that separate identity; primary-subtree paths resolve to ``None``; and editing a separate
  identity raises ``WorkspaceAffinityError`` unless the project's ``workspace_id`` explicitly
  binds that identity.

All fixtures use local git only (no network, model, provider, or fetch) and run on both
Windows and POSIX ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import converge_orchestrator.workspace_identity as workspace_identity

_MISSING = "REQREPO005_SEPARATE_REPOSITORY_IDENTITY_PRIMITIVE_MISSING"


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    _git("init", "-b", "main", str(path))
    _git("config", "user.name", "ci", cwd=path)
    _git("config", "user.email", "ci@example.invalid", cwd=path)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-m", "seed", cwd=path)
    return path


def _resolve_separate_identity(primary_root: Path, target: Path) -> Path | None:
    """Resolve the REQ-REPO-005 classifier, failing with the RED marker if it is absent."""
    primitive = getattr(workspace_identity, "resolve_separate_repository_identity", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.resolve_separate_repository_identity is not defined; "
        "REQ-REPO-005 requires classifying a working path as owned by the primary identity "
        "or by a separate repository identity under the primary tree."
    )
    fn: Callable[[Path, Path], Path | None] = cast("Callable[[Path, Path], Path | None]", primitive)
    return fn(primary_root, target)


def _assert_repository_identity_authority(
    project: dict, primary_root: Path, target: Path
) -> Path | None:
    """Resolve the REQ-REPO-005 authority primitive, failing with the RED marker if absent."""
    primitive = getattr(workspace_identity, "assert_repository_identity_authority", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.assert_repository_identity_authority is not defined; "
        "REQ-REPO-005 requires explicit project workspace binding before editing a separate "
        "repository identity under the primary tree."
    )
    fn: Callable[[dict, Path, Path], Path | None] = cast(
        "Callable[[dict, Path, Path], Path | None]", primitive
    )
    return fn(project, primary_root, target)


def test_nested_repository_inside_primary_resolves_as_separate_identity(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendors" / "nested")

    identity = _resolve_separate_identity(primary, nested)
    assert identity is not None
    assert identity.resolve() == nested.resolve()


def test_submodule_inside_primary_resolves_as_separate_identity(tmp_path: Path) -> None:
    upstream = _git_repo(tmp_path / "upstream")
    primary = _git_repo(tmp_path / "primary")
    submodule_dir = primary / "vendors" / "sub"
    _git("submodule", "add", "--name", "sub", str(upstream), str(submodule_dir), cwd=primary)
    _git("commit", "-m", "add submodule", cwd=primary)

    identity = _resolve_separate_identity(primary, submodule_dir)
    assert identity is not None
    assert identity.resolve() == submodule_dir.resolve()


def test_foreign_linked_worktree_inside_primary_resolves_as_separate_identity(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    foreign = _git_repo(tmp_path / "foreign")
    worktree_path = primary / "vendors" / "foreign-wt"
    _git("worktree", "add", str(worktree_path), cwd=foreign)

    identity = _resolve_separate_identity(primary, worktree_path)
    assert identity is not None
    assert identity.resolve() == worktree_path.resolve()


def test_external_mount_inside_primary_resolves_as_separate_identity(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    external = _git_repo(tmp_path / "external")
    mount_point = primary / "vendors" / "external"
    mount_point.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(mount_point), str(external)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        os.symlink(external, mount_point, target_is_directory=True)

    identity = _resolve_separate_identity(primary, mount_point)
    assert identity is not None
    assert identity.resolve() == external.resolve()


def test_primary_subtree_path_resolves_to_none(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    primary_file = primary / "src" / "main.py"
    primary_file.parent.mkdir(parents=True, exist_ok=True)
    primary_file.write_text("pass\n", encoding="utf-8")

    assert _resolve_separate_identity(primary, primary_file) is None
    assert _resolve_separate_identity(primary, primary / "src") is None


def test_editing_separate_identity_requires_explicit_workspace_binding(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendors" / "nested")
    primary_ws = workspace_identity.workspace_id(primary)
    nested_ws = workspace_identity.workspace_id(nested)

    bound_to_nested = {"id": "project", "workspace_id": nested_ws}
    bound_to_primary = {"id": "project", "workspace_id": primary_ws}
    unbound = {"id": "project", "workspace_id": None}

    assert (
        _assert_repository_identity_authority(bound_to_nested, primary, nested)
        == nested.resolve()
    )

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_repository_identity_authority(bound_to_primary, primary, nested)

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_repository_identity_authority(unbound, primary, nested)


def test_primary_authority_applies_to_primary_subtree(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    primary_ws = workspace_identity.workspace_id(primary)
    primary_file = primary / "src" / "main.py"
    primary_file.parent.mkdir(parents=True, exist_ok=True)
    primary_file.write_text("pass\n", encoding="utf-8")

    bound_to_primary = {"id": "project", "workspace_id": primary_ws}

    assert _assert_repository_identity_authority(bound_to_primary, primary, primary_file) is None

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_repository_identity_authority(
            {"id": "project", "workspace_id": "foreign-workspace"},
            primary,
            primary_file,
        )
