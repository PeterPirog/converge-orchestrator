from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from converge_orchestrator.models import ComplianceSnapshot, GateResult, ProjectConfig, TaskEnvelope
from converge_orchestrator.workflow import integrate


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


def _config(tmp_path: Path, repo: Path) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("immutable\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )


def _state(tmp_path: Path, worktree: Path) -> dict:
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Recover integration",
        objective="Preserve the candidate across a process crash",
        allowed_paths=["src/**"],
    )
    gate = GateResult(
        name="tests",
        ok=True,
        required=True,
        returncode=0,
        output="pass",
    )
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-hash",
        "task": task.model_dump(mode="json"),
        "worktree": str(worktree),
        "branch": "converge/arch-001-1",
        "quality_results": [gate.model_dump(mode="json")],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
        "recorded_canonical_base_revision": "base-sha",
    }


def test_integrate_recovers_commit_created_before_langgraph_checkpoint(
    tmp_path: Path,
) -> None:
    # Create a real Git repository with a candidate commit
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
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")

    # Create initial commit
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial")
    _run_git(repo, "push", "-u", "origin", "main")

    # Create a candidate commit ahead of base
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run_git(repo, "add", "feature.txt")
    _run_git(repo, "commit", "-m", "candidate feature")
    candidate_sha = _run_git(repo, "rev-parse", "HEAD")

    # Create worktree from the candidate commit
    worktree = tmp_path / "worktree"
    _run_git(repo, "worktree", "add", str(worktree), candidate_sha)

    cfg = _config(tmp_path, repo)
    state = _state(tmp_path, worktree)
    # Simulate checkpoint loss: no commit_sha in state
    state.pop("commit_sha", None)

    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push") as push,
    ):
        result = integrate(state)

    # Verify recovery behavior
    assert result["commit_sha"] == candidate_sha
    assert result["status"] == "pushed"
    assert result["message"] == "Recovered existing candidate commit and pushed."
    event = store.append_event.call_args.args[2]
    assert event["recovered_existing_commit"] is True
    assert event["commit_sha"] == candidate_sha
    # Verify compliance is propagated
    assert "compliance" in result
    # Verify push was called
    push.assert_called_once()


def test_integrate_recovers_commit_with_nested_repo_denied(
    tmp_path: Path,
) -> None:
    """Recovery must deny if recovered commit modifies unauthorized nested repo."""
    # Create a real Git repository with a nested git repo
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
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")

    # Create a nested Git repository inside the main repo
    nested = repo / "vendor" / "embedded"
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--initial-branch=main")
    _run_git(nested, "config", "user.email", "nested@example.com")
    _run_git(nested, "config", "user.name", "Nested Repo")
    (nested / "nested_file.txt").write_text("nested content\n", encoding="utf-8")
    _run_git(nested, "add", "nested_file.txt")
    _run_git(nested, "commit", "-m", "nested initial")

    # Create initial commit in main repo
    (repo / "README.md").write_text("main repo\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial")
    _run_git(repo, "push", "-u", "origin", "main")

    # Create a candidate commit that modifies the nested repo
    # First, create the nested directory in the main repo worktree
    path = repo / "vendor" / "embedded" / "unauthorized.txt"
    path.write_text("unauthorized\n", encoding="utf-8")
    _run_git(repo, "add", "vendor/embedded/unauthorized.txt")
    _run_git(repo, "commit", "-m", "modify nested")
    candidate_sha = _run_git(repo, "rev-parse", "HEAD")

    # Create worktree from the candidate commit
    worktree = tmp_path / "worktree"
    _run_git(repo, "worktree", "add", str(worktree), candidate_sha)

    cfg = _config(tmp_path, repo)
    state = _state(tmp_path, worktree)
    state.pop("commit_sha", None)

    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push"),
    ):
        result = integrate(state)

    # Should be denied due to unauthorized nested repo modification
    assert result["status"] == "nested_authority_denied"
    assert result["commit_sha"] is None
    assert "vendor/embedded" in result["message"]
    assert "primary_target_authority_not_transitive_to_nested_identity" in result["message"]
    assert "nested_identity_explicit_grant_missing_or_mismatched" in result["message"]


def test_integrate_recovers_commit_with_nested_repo_allowed(
    tmp_path: Path,
) -> None:
    """Recovery must allow if recovered commit modifies authorized nested repo."""
    # Create a real Git repository with a nested git repo
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
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")

    # Create a nested Git repository inside the main repo
    nested = repo / "vendor" / "embedded"
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--initial-branch=main")
    _run_git(nested, "config", "user.email", "nested@example.com")
    _run_git(nested, "config", "user.name", "Nested Repo")
    (nested / "nested_file.txt").write_text("nested content\n", encoding="utf-8")
    _run_git(nested, "add", "nested_file.txt")
    _run_git(nested, "commit", "-m", "nested initial")

    # Create initial commit in main repo
    (repo / "README.md").write_text("main repo\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "initial")
    _run_git(repo, "push", "-u", "origin", "main")

    # Create a candidate commit that modifies the nested repo
    path = repo / "vendor" / "embedded" / "authorized.txt"
    path.write_text("authorized\n", encoding="utf-8")
    _run_git(repo, "add", "vendor/embedded/authorized.txt")
    _run_git(repo, "commit", "-m", "modify nested")
    candidate_sha = _run_git(repo, "rev-parse", "HEAD")

    # Create worktree from the candidate commit
    worktree = tmp_path / "worktree"
    _run_git(repo, "worktree", "add", str(worktree), candidate_sha)

    cfg = _config(tmp_path, repo)
    # Grant authority for the nested repo
    cfg.project_authority_grants = {"vendor/embedded": "nested_git_repository"}

    state = _state(tmp_path, worktree)
    state.pop("commit_sha", None)

    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push"),
    ):
        result = integrate(state)

    # Should be allowed with exact grant
    assert result["status"] == "pushed"
    assert result["commit_sha"] is not None
    assert result["message"] == "Recovered existing candidate commit and pushed."


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])