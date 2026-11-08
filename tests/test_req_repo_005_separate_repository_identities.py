"""Tests for REQ-REPO-005: nested repository identities remain separate.

Nested Git repositories, submodules, linked worktrees, and external repository mounts
must remain separate repository identities. ``commit_all`` must refuse to commit any
candidate that stages an embedded nested-repository gitlink (index mode ``160000``) or
mutates any path under a detected secondary repository root inside the candidate worktree,
and must leave both the primary target repository and the nested repository uncommitted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.git import GitError, commit_all, create_worktree


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def _git_stdout(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout


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
    _run_git(repo, "config", "user.email", "converge@example.invalid")
    _run_git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    return repo


def _worktree(repo: Path, tmp_path: Path) -> Path:
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", "ARCH-001-1", "main")
    return worktree


def _init_nested_repository(parent: Path, rel_path: str) -> Path:
    """Initialize an embedded git repository inside *parent* at *rel_path*."""

    nested = parent / rel_path
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--initial-branch=main")
    _run_git(nested, "config", "user.email", "converge@example.invalid")
    _run_git(nested, "config", "user.name", "Converge Test")
    (nested / "nested.txt").write_text("nested baseline\n", encoding="utf-8")
    _run_git(nested, "add", "nested.txt")
    _run_git(nested, "commit", "-m", "nested baseline")
    # Leave an unstaged change so the test can verify the nested repo is not committed.
    (nested / "change.txt").write_text("nested change\n", encoding="utf-8")
    return nested


def test_commit_all_rejects_untracked_embedded_nested_repository(tmp_path: Path) -> None:
    """An untracked embedded repository must not be swallowed into the primary commit."""

    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    nested = _init_nested_repository(worktree, "vendor/subrepo")

    status = _git_stdout(worktree, "status", "--porcelain", "-uall")
    assert "?? vendor/subrepo/" in status

    with pytest.raises(
        GitError, match="REQ-REPO-005 secondary repository mutation rejected"
    ):
        commit_all(worktree, "feat: candidate with nested repo")

    # Primary repository must not have created any candidate commit.
    assert _run_git(worktree, "rev-list", "--count", "origin/main..HEAD") == "0"
    # Nested repository must remain uncommitted.
    nested_status = _git_stdout(nested, "status", "--porcelain", "-uall")
    assert "change.txt" in nested_status


def test_commit_all_commits_plain_repository_changes(tmp_path: Path) -> None:
    """Ordinary plain-repository candidate changes must still commit normally."""

    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    (worktree / "feature.txt").write_text("candidate\n", encoding="utf-8")

    commit = commit_all(worktree, "feat: candidate")

    assert commit is not None
    assert commit == _run_git(worktree, "rev-parse", "HEAD")
    assert _git_stdout(worktree, "status", "--porcelain", "-uall") == ""
    committed = _run_git(worktree, "show", "--name-only", "--format=")
    assert "feature.txt" in committed.splitlines()
