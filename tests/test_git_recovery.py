from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from converge_orchestrator.git import (
    GitError,
    commit_all,
    create_worktree,
    existing_candidate_commit,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _git(repo, "config", "user.email", "converge@example.invalid")
    _git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def test_create_worktree_adopts_matching_candidate_without_destroying_changes(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")
    candidate = worktree / "candidate.txt"
    candidate.write_text("unsaved autonomous work\n", encoding="utf-8")

    resumed_worktree, resumed_branch = create_worktree(
        repo,
        root,
        "ARCH-001-1",
        "main",
    )

    assert resumed_worktree == worktree
    assert resumed_branch == branch
    assert candidate.read_text(encoding="utf-8") == "unsaved autonomous work\n"


def test_create_worktree_fails_closed_on_unregistered_existing_path(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    foreign = root / "arch-001-1"
    foreign.mkdir(parents=True)
    (foreign / "do-not-delete.txt").write_text("operator data\n", encoding="utf-8")

    with pytest.raises(GitError, match="not a registered branch worktree"):
        create_worktree(repo, root, "ARCH-001-1", "main")

    assert (foreign / "do-not-delete.txt").read_text(encoding="utf-8") == "operator data\n"


def test_create_worktree_fails_closed_when_registered_branch_changed(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, _ = create_worktree(repo, root, "ARCH-001-1", "main")
    _git(worktree, "checkout", "-b", "operator/foreign")

    with pytest.raises(GitError, match="belongs to operator/foreign"):
        create_worktree(repo, root, "ARCH-001-1", "main")


def test_existing_candidate_commit_recovers_commit_created_before_checkpoint(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", "ARCH-001-1", "main")
    assert existing_candidate_commit(worktree, "main") is None

    (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    commit = commit_all(worktree, "feat: candidate")

    assert commit is not None
    assert existing_candidate_commit(worktree, "main") == commit
