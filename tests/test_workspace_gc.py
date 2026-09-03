from __future__ import annotations

import subprocess
from pathlib import Path

from converge_orchestrator.git import (
    cleanup_worktree,
    create_worktree,
    garbage_collect_requested_worktrees,
)
from converge_orchestrator.workspace_ownership import WorkspaceOwnershipStore


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


def test_create_worktree_persists_active_ownership(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"

    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")

    record = WorkspaceOwnershipStore(root).read(branch)
    assert record is not None
    assert record.status == "active"
    assert record.task_id == "ARCH-001-1"
    assert Path(record.path) == worktree


def test_gc_never_removes_active_workspace(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, _ = create_worktree(repo, root, "ARCH-001-1", "main")

    results = garbage_collect_requested_worktrees(repo, root, dry_run=False)

    assert results == []
    assert worktree.is_dir()


def test_gc_finishes_cleanup_requested_before_crash(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")
    store = WorkspaceOwnershipStore(root)
    store.request_cleanup(
        target=worktree,
        branch=branch,
        reason="simulated_crash_window",
    )

    preview = garbage_collect_requested_worktrees(repo, root, dry_run=True)
    assert preview[0]["status"] == "would_release"
    assert worktree.exists()

    applied = garbage_collect_requested_worktrees(repo, root, dry_run=False)

    assert applied[0]["status"] == "released"
    assert not worktree.exists()
    record = store.read(branch)
    assert record is not None
    assert record.status == "released"


def test_gc_refuses_unregistered_operator_directory(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    foreign = root / "arch-foreign"
    foreign.mkdir(parents=True)
    marker = foreign / "operator-data.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    branch = "converge/arch-foreign"
    WorkspaceOwnershipStore(root).request_cleanup(
        target=foreign,
        branch=branch,
        reason="fixture",
    )

    results = garbage_collect_requested_worktrees(repo, root, dry_run=False)

    assert results[0]["status"] == "blocked"
    assert "unregistered filesystem path" in results[0]["reason"]
    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_gc_refuses_branch_reassigned_inside_registered_worktree(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")
    _git(worktree, "checkout", "-b", "operator/foreign")
    WorkspaceOwnershipStore(root).request_cleanup(
        target=worktree,
        branch=branch,
        reason="fixture",
    )

    results = garbage_collect_requested_worktrees(repo, root, dry_run=False)

    assert results[0]["status"] == "blocked"
    assert "belongs to operator/foreign" in results[0]["reason"]
    assert worktree.exists()


def test_controlled_cleanup_marks_workspace_released(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    root = tmp_path / "worktrees"
    worktree, branch = create_worktree(repo, root, "ARCH-001-1", "main")

    cleanup_worktree(repo, worktree, branch, reason="merged")

    record = WorkspaceOwnershipStore(root).read(branch)
    assert record is not None
    assert record.status == "released"
    assert not worktree.exists()
    events = [item["event"] for item in record.history]
    assert "cleanup_requested:merged" in events
    assert events[-1] == "released"
