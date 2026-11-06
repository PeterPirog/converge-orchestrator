from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from converge_orchestrator.git import (
    GitError,
    _remove_untracked_cache_artifacts,
    cleanup_worktree,
    create_worktree,
)
from converge_orchestrator.workspace_ownership import WorkspaceOwnershipError


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "config", "user.email", "converge@example.invalid")
    _git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _make_dir_alias(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_dir_alias(link: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        os.unlink(link)


@contextlib.contextmanager
def _alias_dir(link: Path, target: Path):
    _make_dir_alias(link, target)
    try:
        yield
    finally:
        try:
            _remove_dir_alias(link)
        except OSError:
            pass


def _boundary_exc(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except (GitError, WorkspaceOwnershipError) as exc:
        return str(exc)
    except Exception as exc:
        pytest.fail(
            f"REQ-REPO-003 boundary escape rejected: unexpected {type(exc).__name__}: {exc}"
        )
    pytest.fail("REQ-REPO-003 boundary escape rejected: call escaped authorized boundary")


def test_create_worktree_rejects_aliased_worktree_root(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = authorized / "worktrees"
    with _alias_dir(alias, outside):
        msg = _boundary_exc(create_worktree, repo, alias, "ARCH-001-1", "main")
    assert "REQ-REPO-003 boundary escape rejected" in msg
    assert not any(outside.iterdir()), (
        "REQ-REPO-003 boundary escape rejected: create_worktree mutated outside root"
    )


def test_cleanup_worktree_rejects_aliased_target_escape(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "payload"
    payload.mkdir()
    marker = payload / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    shutil.rmtree(worktree)
    with _alias_dir(worktree, payload):
        msg = _boundary_exc(cleanup_worktree, repo, worktree, branch, reason="boundary_test")
    assert marker.exists(), (
        "REQ-REPO-003 boundary escape rejected: cleanup_worktree deleted outside root"
    )
    assert not (outside / ".ownership").exists(), (
        "REQ-REPO-003 boundary escape rejected: cleanup_worktree created ownership outside root"
    )
    assert "REQ-REPO-003 boundary escape rejected" in msg


def test_cleanup_worktree_rejects_traversal_target_escape(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    traversal = root / "arch-001-1" / ".." / ".." / "outside"
    msg = _boundary_exc(cleanup_worktree, repo, traversal, branch, reason="boundary_test")
    assert marker.exists(), (
        "REQ-REPO-003 boundary escape rejected: cleanup_worktree deleted outside root"
    )
    assert not (tmp_path / ".ownership").exists(), (
        "REQ-REPO-003 boundary escape rejected: cleanup_worktree created ownership outside root"
    )
    assert "REQ-REPO-003 boundary escape rejected" in msg


def test_remove_untracked_cache_artifacts_rejects_aliased_worktree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, _ = create_worktree(repo, root, "ARCH-001-1", "main")
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = outside / "__pycache__"
    cache.mkdir()
    marker = cache / "keep.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    subprocess.run(["git", "init", str(outside)], check=True, capture_output=True, text=True)
    shutil.rmtree(worktree)
    with _alias_dir(worktree, outside):
        msg = _boundary_exc(_remove_untracked_cache_artifacts, worktree)
    assert "REQ-REPO-003 boundary escape rejected" in msg
    assert marker.exists(), (
        "REQ-REPO-003 boundary escape rejected: cache cleanup deleted outside root"
    )
