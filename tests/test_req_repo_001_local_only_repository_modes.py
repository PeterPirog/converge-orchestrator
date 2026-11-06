"""RED-phase tests for REQ-REPO-001 local-only Git repository support.

These tests exercise the base-anchored Git layer when a repository has commits but
no ``origin`` remote. They are intentionally written against the *current* production
implementation, which still resolves every base reference through
``origin/{base_branch}`` and therefore fails with :class:`GitError` until the
git adapter learns to fall back to local refs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from converge_orchestrator.git import (
    changed_files,
    create_worktree,
    diff,
    diff_line_count,
    existing_candidate_commit,
    update_base,
)
from converge_orchestrator.models import ProjectConfig, TaskEnvelope, TDDPlan
from converge_orchestrator.risk import classify_repository_risk


def _run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _local_only_repository(tmp_path: Path) -> Path:
    """Create a committed repository with no origin remote."""

    repo = tmp_path / "local_repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(repo, "config", "user.email", "converge@example.invalid")
    _run_git(repo, "config", "user.name", "Converge Test")
    (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", "baseline.txt")
    _run_git(repo, "commit", "-m", "baseline")
    return repo


def _origin_connected_repository(tmp_path: Path) -> Path:
    """Create a clone with an origin remote, exactly like the existing git tests."""

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


def test_update_base_returns_local_head_when_origin_remote_is_missing(
    tmp_path: Path,
) -> None:
    repo = _local_only_repository(tmp_path)
    assert _run_git(repo, "remote") == ""

    local_head = _run_git(repo, "rev-parse", "HEAD")
    result = update_base(repo, "main")

    assert result == local_head


def test_create_worktree_branches_candidate_from_local_base_when_origin_ref_missing(
    tmp_path: Path,
) -> None:
    repo = _local_only_repository(tmp_path)
    base_commit = _run_git(repo, "rev-parse", "HEAD")

    worktree, branch = create_worktree(
        repo, tmp_path / "worktrees", "REQ-REPO-001-0038", "main"
    )

    assert worktree.exists()
    assert branch == "converge/req-repo-001-0038"
    assert _run_git(worktree, "rev-parse", "HEAD") == base_commit
    assert _run_git(worktree, "remote") == ""


def test_changed_files_diff_and_diff_line_count_report_local_only_worktree_changes(
    tmp_path: Path,
) -> None:
    repo = _local_only_repository(tmp_path)
    # The main checkout of a local-only repository is a valid worktree for these helpers.
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("one\ntwo\n", encoding="utf-8")

    files = changed_files(repo, "main")
    assert "tracked.txt" in files
    assert "untracked.txt" in files

    patch = diff(repo, "main")
    assert "tracked.txt" in patch
    assert "untracked.txt" in patch

    assert diff_line_count(repo, "main") >= 3


def test_existing_candidate_commit_counts_commits_over_local_base_branch(
    tmp_path: Path,
) -> None:
    repo = _local_only_repository(tmp_path)
    _run_git(repo, "checkout", "-b", "candidate")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run_git(repo, "add", "feature.txt")
    _run_git(repo, "commit", "-m", "candidate feature")

    expected = _run_git(repo, "rev-parse", "HEAD")
    assert existing_candidate_commit(repo, "main") == expected


def test_update_base_prefers_origin_ref_when_remote_present(tmp_path: Path) -> None:
    """Regression: do not break the existing fetch/pull path for origin-connected repos."""

    repo = _origin_connected_repository(tmp_path)
    local_before = _run_git(repo, "rev-parse", "HEAD")

    # Advance origin/main from a second clone.
    upstream = tmp_path / "upstream"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(upstream)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git(upstream, "config", "user.email", "converge@example.invalid")
    _run_git(upstream, "config", "user.name", "Converge Test")
    (upstream / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _run_git(upstream, "add", "upstream.txt")
    _run_git(upstream, "commit", "-m", "upstream change")
    _run_git(upstream, "push", "origin", "main")

    result = update_base(repo, "main")
    origin_head = _run_git(repo, "rev-parse", "origin/main")

    assert result == origin_head
    assert result != local_before


def test_risk_classification_reads_base_without_git_error_for_local_only(
    tmp_path: Path,
) -> None:
    repo = _local_only_repository(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def service() -> int:\n    return 1\n", encoding="utf-8"
    )
    _run_git(repo, "add", "src/service.py")
    _run_git(repo, "commit", "-m", "add service")

    requirements = tmp_path / "requirements.md"
    requirements.write_text("System must support local-only repositories.\n", encoding="utf-8")
    config = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        base_branch="main",
        require_spec_read_only=False,
        agents={},
        auto_discover_quality=False,
        quality_gates=[],
    )
    task = TaskEnvelope(
        id="REQ-REPO-001-0038",
        requirement_ids=["REQ-REPO-001"],
        title="Support local-only Git repositories",
        objective="Local-only repositories must pass risk base reads without GitError.",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd=TDDPlan(
            mode="required",
            test_paths=["tests/**"],
            expected_failure_pattern="test_update_base_returns_local_head_when_origin_remote_is_missing",
            rationale="Observable git adapter behavior change.",
        ),
    )

    report = classify_repository_risk(config, repo, task)
    assert isinstance(report.findings, list)
