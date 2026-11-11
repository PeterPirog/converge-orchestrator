"""RED lock test for REQ-REPO-005: separate repository identities remain separate.

REQ-REPO-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes,
greenfield bootstrap, and brownfield discovery]): Nested Git repositories, submodules,
linked worktrees, and external repository mounts SHALL remain separate repository
identities; editing their internal contents SHALL require explicit project authority
rather than being implied by authority over the primary target repository.

These deterministic offline tests lock the not-yet-existing primitives
``repository_identity_root`` and ``assert_edit_repository_authority`` in
``converge_orchestrator.workspace_identity``:

- against the old implementation the module has no such symbols, so every test fails
  with the literal marker ``REQ-REPO-005_REPOSITORY_IDENTITY_PRIMITIVE_MISSING``;
- once the primitives are added, the same tests must pass: ordinary primary-repo
  content resolves to the primary toplevel, nested repos / submodules / linked
  worktrees / external mounts each resolve to a distinct identity root, and the
  guard rejects edits there unless the project mapping explicitly authorizes that
  root.

All fixtures use local git only (no network, model, provider, or fetch).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import converge_orchestrator.workspace_identity as workspace_identity

_MISSING = "REQ-REPO-005_REPOSITORY_IDENTITY_PRIMITIVE_MISSING"


def _run_git(*args: str, cwd: Path | None = None) -> None:
    env = os.environ.copy()
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = "protocol.file.allow"
    env[f"GIT_CONFIG_VALUE_{count}"] = "always"
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
        env=env,
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


def _repository_identity_root(edit_path: Path) -> Path:
    """Resolve the REQ-REPO-005 identity-root primitive, failing RED if absent."""
    primitive = getattr(workspace_identity, "repository_identity_root", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.repository_identity_root is not defined; "
        "REQ-REPO-005 requires resolving the Git repository identity root enclosing an edit path."
    )
    root_fn: Callable[[Path], Path] = cast("Callable[[Path], Path]", primitive)
    return root_fn(edit_path)


def _assert_edit_repository_authority(
    project: dict[str, Any],
    edit_path: Path,
    primary_repo: Path,
) -> Path:
    """Resolve the REQ-REPO-005 authority guard, failing RED if absent."""
    primitive = getattr(workspace_identity, "assert_edit_repository_authority", None)
    assert callable(primitive), (
        f"{_MISSING}: workspace_identity.assert_edit_repository_authority is not defined; "
        "REQ-REPO-005 requires an explicit-authority guard before editing separate-identity "
        "repository contents."
    )
    guard: Callable[[dict[str, Any], Path, Path], Path] = cast(
        "Callable[[dict[str, Any], Path, Path], Path]", primitive
    )
    return guard(project, edit_path, primary_repo)


def _project(primary_repo: Path, authorized: set[Path] | None = None) -> dict[str, Any]:
    roots: dict[str, bool] = {}
    for root in authorized or ():
        roots[str(root.resolve())] = True
    return {
        "id": "test-project",
        "repo_path": str(primary_repo),
        "authorized_repository_roots": roots,
    }


def _mount_directory(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_identity_root_equals_primary_toplevel_for_ordinary_path(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    assert _repository_identity_root(primary / "seed.txt") == primary


def test_nested_git_repository_resolves_to_distinct_root_and_rejects_without_authority(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    edit_path = nested / "file.txt"

    assert _repository_identity_root(edit_path) == nested
    assert _repository_identity_root(edit_path) != primary

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(_project(primary), edit_path, primary)

    authorized = _project(primary, authorized={nested})
    assert _assert_edit_repository_authority(authorized, edit_path, primary) == nested


def test_submodule_resolves_to_distinct_root_and_rejects_without_authority(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    sub_origin = _git_repo(tmp_path / "sub_origin")
    _run_git("submodule", "add", str(sub_origin), "vendor/sub", cwd=primary)
    _run_git("commit", "-m", "add submodule", cwd=primary)
    sub = primary / "vendor" / "sub"
    (sub / "file.txt").write_text("sub\n", encoding="utf-8")
    edit_path = sub / "file.txt"

    assert _repository_identity_root(edit_path) == sub
    assert _repository_identity_root(edit_path) != primary

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(_project(primary), edit_path, primary)

    authorized = _project(primary, authorized={sub})
    assert _assert_edit_repository_authority(authorized, edit_path, primary) == sub


def test_linked_worktree_resolves_to_distinct_root_and_rejects_without_authority(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    worktree = tmp_path / "primary-worktree"
    _run_git("worktree", "add", str(worktree), cwd=primary)
    edit_path = worktree / "seed.txt"

    assert _repository_identity_root(edit_path) == worktree
    assert _repository_identity_root(edit_path) != primary

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(_project(primary), edit_path, primary)

    authorized = _project(primary, authorized={worktree})
    assert _assert_edit_repository_authority(authorized, edit_path, primary) == worktree


def test_external_mounted_repository_resolves_to_distinct_root_and_rejects_without_authority(
    tmp_path: Path,
) -> None:
    primary = _git_repo(tmp_path / "primary")
    external = _git_repo(tmp_path / "external")
    mount_point = primary / "mount" / "external"
    _mount_directory(mount_point, external)
    edit_path = mount_point / "seed.txt"

    assert _repository_identity_root(edit_path) == external
    assert _repository_identity_root(edit_path) != primary

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(_project(primary), edit_path, primary)

    authorized = _project(primary, authorized={external})
    assert _assert_edit_repository_authority(authorized, edit_path, primary) == external


def test_malformed_unresolvable_path_raises(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    outside = tmp_path / "outside" / "nowhere.txt"

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _repository_identity_root(outside)

    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(_project(primary), outside, primary)


def test_missing_authorization_mapping_raises_for_separate_identity(tmp_path: Path) -> None:
    primary = _git_repo(tmp_path / "primary")
    nested = _git_repo(primary / "vendor" / "nested")
    edit_path = nested / "file.txt"

    project_without_authority: dict[str, Any] = {"id": "test-project", "repo_path": str(primary)}
    with pytest.raises(workspace_identity.WorkspaceAffinityError):
        _assert_edit_repository_authority(project_without_authority, edit_path, primary)
