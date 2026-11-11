"""RED-phase tests for REQ-REPO-005 nested repository authority in commit_all.

REQ-REPO-005 requires nested Git identities (nested ``.git`` directories, submodule
``.git`` pointers, linked worktrees, and external repository mounts) to remain
separate repository identities.  ``commit_all`` must therefore fail-closed, before
any staging, when a candidate worktree contains a nested Git identity whose
internal contents have been edited, unless explicit project authority over that
nested identity is declared.

These tests are written against the current production implementation, which does
not yet enforce the guard.  The edited-nested-identity test fails with the
literal RED marker until the guard is added.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.git import (
    GitError,
    commit_all,
    create_worktree,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    """Create an origin-connected repository with a baseline commit."""

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


def _worktree(repo: Path, tmp_path: Path) -> Path:
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", "REQ-REPO-005-0038", "main")
    return worktree


def _init_nested_identity(path: Path) -> None:
    """Initialize an independent Git identity inside the candidate worktree."""

    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "converge@example.invalid")
    _git(path, "config", "user.name", "Converge Test")
    (path / "internal.txt").write_text("nested baseline\n", encoding="utf-8")
    _git(path, "add", "internal.txt")
    _git(path, "commit", "-m", "nested baseline")


def test_commit_all_plain_repository_still_succeeds(tmp_path: Path) -> None:
    """No nested identity => unchanged behavior, commit_all publishes real changes."""

    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    (worktree / "feature.txt").write_text("candidate change\n", encoding="utf-8")

    commit = commit_all(worktree, "feat: candidate")

    assert commit is not None
    assert commit == _git(worktree, "rev-parse", "HEAD")
    assert _git(worktree, "status", "--porcelain") == ""


def test_commit_all_allows_pristine_nested_identity(tmp_path: Path) -> None:
    """A present but unmodified nested identity does not trigger the guard."""

    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    nested = worktree / "vendor" / "lib"
    _init_nested_identity(nested)
    assert _git(nested, "status", "--porcelain") == ""

    (worktree / "feature.txt").write_text("candidate change\n", encoding="utf-8")

    commit = commit_all(worktree, "feat: candidate with pristine nested identity")

    assert commit is not None
    assert commit == _git(worktree, "rev-parse", "HEAD")
    assert _git(worktree, "status", "--porcelain") == ""


def test_commit_all_rejects_edited_nested_identity(tmp_path: Path) -> None:
    """An edited nested Git identity must be rejected before any staging."""

    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    nested = worktree / "vendor" / "lib"
    _init_nested_identity(nested)
    (nested / "internal.txt").write_text("nested candidate edit\n", encoding="utf-8")

    # Evidence: the nested identity itself sees the internal edit.
    assert _git(nested, "status", "--porcelain") != ""

    head_before = _git(worktree, "rev-parse", "HEAD")

    try:
        commit_all(worktree, "feat: candidate with edited nested identity")
    except GitError as exc:
        message = str(exc)
        assert "REQ-REPO-005 nested repository edit rejected" in message
        assert _git(worktree, "rev-parse", "HEAD") == head_before
        assert _git(worktree, "diff", "--cached", "--name-only", "HEAD") == ""
        return

    pytest.fail(
        "REQ-REPO-005 nested repository edit requires explicit project authority"
    )
