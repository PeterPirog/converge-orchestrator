"""RED-phase tests for REQ-REPO-005: separate repository identities.

Nested Git repositories, submodules, linked worktrees, and external repository mounts
inside a primary target SHALL remain separate repository identities.

These tests lock ``converge_orchestrator.workspace_identity.resolve_workspace_identity``.
Against the old implementation it is missing, so every test fails with the literal marker
``REQ005_SEPARATE_REPOSITORY_IDENTITY_GUARD_MISSING``.

All fixtures use local git only (no network, model, provider, or fetch).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import converge_orchestrator.workspace_identity as workspace_identity

_MISSING = "REQ005_SEPARATE_REPOSITORY_IDENTITY_GUARD_MISSING"


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.name", "ci")
    _run_git(path, "config", "user.email", "ci@example.invalid")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-m", "seed")
    return path


def _junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        os.symlink(target.resolve(), link, target_is_directory=True)


def _resolve_workspace_identity(repo: Path, target: Path) -> dict[str, Any]:
    """Call the REQ-REPO-005 primitive, failing with the RED marker if absent."""
    primitive = getattr(workspace_identity, "resolve_workspace_identity", None)
    if not callable(primitive):
        pytest.fail(
            f"{_MISSING}: workspace_identity.resolve_workspace_identity(repo, path) "
            "is not defined; REQ-REPO-005 requires an offline, fail-closed workspace "
            "identity primitive that reports distinct-identity clauses for nested "
            "repositories, submodules, linked worktrees, and external mounts."
        )
    return cast("dict[str, Any]", primitive(repo, target))


def _assert_distinct_identity(repo: Path, target: Path, reason: str) -> None:
    result = _resolve_workspace_identity(repo, target)
    primary = _resolve_workspace_identity(repo, repo)["identity"]
    distinct = result.get("distinct_identity")
    assert distinct is not None
    assert distinct["reason"] == reason
    assert distinct["identity"] != primary
    assert result["identity"] == distinct["identity"]


def test_primary_target_path_has_no_distinct_identity_clause(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    result = _resolve_workspace_identity(repo, repo / "seed.txt")

    assert isinstance(result.get("identity"), str) and result["identity"]
    assert result.get("distinct_identity") is None
    assert _resolve_workspace_identity(repo, repo)["identity"] == result["identity"]


def test_nested_git_repository_inside_primary_reports_distinct_identity(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    nested = _git_repo(repo / "vendor" / "nested")
    _assert_distinct_identity(repo, nested / "seed.txt", "nested_repository")


def test_submodule_inside_primary_reports_distinct_identity(tmp_path: Path) -> None:
    sub_origin = _git_repo(tmp_path / "sub_origin")
    repo = _git_repo(tmp_path / "repo")
    # Local file transport is disabled by default for submodules in recent Git.
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub_origin), "submod"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(repo, "commit", "-m", "add submodule")
    _assert_distinct_identity(repo, repo / "submod" / "seed.txt", "submodule")


def test_linked_worktree_pointer_inside_primary_reports_distinct_identity(
    tmp_path: Path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    wt_path = tmp_path / "linked_wt"
    _run_git(repo, "worktree", "add", str(wt_path))
    link = repo / "mounted_wt"
    _junction(link, wt_path)
    _assert_distinct_identity(repo, link / "seed.txt", "linked_worktree")


def test_external_repository_mount_inside_primary_reports_distinct_identity(
    tmp_path: Path,
) -> None:
    external = _git_repo(tmp_path / "external")
    repo = _git_repo(tmp_path / "repo")
    link = repo / "mount"
    _junction(link, external)
    _assert_distinct_identity(repo, link / "seed.txt", "external_mount")
