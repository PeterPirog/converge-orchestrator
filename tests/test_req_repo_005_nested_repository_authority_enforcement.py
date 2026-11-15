"""Integration tests for REQ-REPO-005 nested repository identity authority enforcement.

These tests verify that the evaluate_nested_repository_identity_authority predicate
is actually enforced on the integrate() mutation path, not just tested in isolation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from converge_orchestrator.git import create_worktree
from converge_orchestrator.models import (
    ComplianceSnapshot,
    GateResult,
    ProjectConfig,
    TaskEnvelope,
)
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


def _git_stdout(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout


def _repository_with_nested_git(tmp_path: Path) -> tuple[Path, Path]:
    """Create a repo with a nested Git repository inside it."""
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

    # Create a nested Git repository inside the main repo
    nested = repo / "vendor" / "embedded"
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--initial-branch=main")
    _run_git(nested, "config", "user.email", "nested@example.invalid")
    _run_git(nested, "config", "user.name", "Nested Repo")
    (nested / "nested_file.txt").write_text("nested content\n", encoding="utf-8")
    _run_git(nested, "add", "nested_file.txt")
    _run_git(nested, "commit", "-m", "nested initial")

    # Add a file in the main repo
    (repo / "README.md").write_text("main repo\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "main initial")
    _run_git(repo, "push", "-u", "origin", "main")

    return repo, nested


def _worktree(repo: Path, tmp_path: Path, task_id: str) -> Path:
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", task_id, "main")
    # Create nested directory structure in worktree (nested git repos are not checked out)
    nested_dir = worktree / "vendor" / "embedded"
    nested_dir.mkdir(parents=True, exist_ok=True)
    return worktree


def _worktree_with_nested_in_worktree(tmp_path: Path, repo: Path, task_id: str) -> Path:
    """Create a worktree and add a nested git repo inside it (simulating worktree outside repo)."""
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", task_id, "main")
    # Create a nested git repo inside the worktree (not in the main repo)
    nested = worktree / "nested_in_wt"
    nested.mkdir(parents=True)
    _run_git(nested, "init", "--initial-branch=main")
    _run_git(nested, "config", "user.email", "nested_wt@example.invalid")
    _run_git(nested, "config", "user.name", "Nested in Worktree")
    (nested / "nested_file.txt").write_text("nested in wt\n", encoding="utf-8")
    _run_git(nested, "add", "nested_file.txt")
    _run_git(nested, "commit", "-m", "nested in wt initial")
    return worktree


def _base_state(
    tmp_path: Path, repo: Path, worktree: Path, task_id: str, cfg: ProjectConfig
) -> dict:
    """Create a base workflow state for integrate()."""
    requirements = tmp_path / "requirements.md"
    requirements.write_text("immutable\n", encoding="utf-8")
    task = TaskEnvelope(
        id=task_id,
        requirement_ids=["REQ-REPO-005"],
        title="Test nested repository authority",
        objective="Verify nested repository authority enforcement",
        allowed_paths=["**"],
        tdd={
            "mode": "not_applicable",
            "test_paths": ["tests/**"],
            "test_gate": None,
            "expected_failure_pattern": "no failing test pattern applicable",
            "rationale": "Test nested repository authority",
        },
    )
    gate = GateResult(name="target-test-suite", ok=True, required=True, returncode=0, output="pass")
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-hash",
        "task": task.model_dump(mode="json"),
        "worktree": str(worktree),
        "branch": f"converge/{task_id.lower()}",
        "quality_results": [gate.model_dump(mode="json")],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
    }


def test_integrate_denies_nested_git_repository_without_explicit_grant(
    tmp_path: Path,
) -> None:
    """integrate() must fail closed when a nested Git repo is edited without grant."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree(repo, tmp_path, "REQ-REPO-005-1")

    # Edit a file inside the nested Git repository (simulating unauthorized edit)
    path = worktree / "vendor" / "embedded" / "unauthorized.txt"
    path.write_text("unauthorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        # No project_authority_grants for vendor/embedded
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-1", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    # Should be denied with nested_authority_denied status
    assert result["status"] == "nested_authority_denied"
    assert result["commit_sha"] is None
    assert "Nested repository identity authority denied" in result["message"]
    assert "vendor/embedded" in result["message"]
    assert "primary_target_authority_not_transitive_to_nested_identity" in result["message"]
    assert "nested_identity_explicit_grant_missing_or_mismatched" in result["message"]


def test_integrate_allows_nested_git_repository_with_exact_explicit_grant(
    tmp_path: Path,
) -> None:
    """integrate() must allow edits when explicit project authority grant
    matches exactly."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree(repo, tmp_path, "REQ-REPO-005-2")

    # Edit a file inside the nested Git repository
    path = worktree / "vendor" / "embedded" / "authorized.txt"
    path.write_text("authorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        project_authority_grants={"vendor/embedded": "nested_git_repository"},
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-2", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push"),  # Don't actually push
    ):
        result = integrate(state)

    # Should succeed (pushed status) because grant matches exactly
    assert result["status"] == "pushed"
    assert result["commit_sha"] is not None


def test_integrate_allows_ordinary_edits_in_primary_repository(tmp_path: Path) -> None:
    """Ordinary authorized edits in the primary target repository must continue to work."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree(repo, tmp_path, "REQ-REPO-005-5")

    # Edit a file in the primary repository (not in nested)
    (worktree / "new_feature.txt").write_text("new feature\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        # No grants needed for primary repository edits
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-5", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push"),
    ):
        result = integrate(state)

    # Should succeed
    assert result["status"] == "pushed"
    assert result["commit_sha"] is not None


def test_integrate_denies_mismatched_grant(tmp_path: Path) -> None:
    """A grant for a different identity class must not satisfy the exact-match requirement."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree(repo, tmp_path, "REQ-REPO-005-6")

    path = worktree / "vendor" / "embedded" / "unauthorized.txt"
    path.write_text("unauthorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        # Grant for wrong identity class
        project_authority_grants={"vendor/embedded": "submodule"},
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-6", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    # Should be denied due to mismatched grant
    assert result["status"] == "nested_authority_denied"
    assert "vendor/embedded" in result["message"]
    assert "nested_identity_explicit_grant_missing_or_mismatched" in result["message"]


def test_integrate_denies_empty_grant(tmp_path: Path) -> None:
    """An empty-string grant must be treated as malformed and fail closed."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree(repo, tmp_path, "REQ-REPO-005-7")

    path = worktree / "vendor" / "embedded" / "unauthorized.txt"
    path.write_text("unauthorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        project_authority_grants={"vendor/embedded": ""},
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-7", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    # Should be denied due to empty grant
    assert result["status"] == "nested_authority_denied"
    assert "vendor/embedded" in result["message"]
    assert "nested_identity_explicit_grant_missing_or_mismatched" in result["message"]


def test_integrate_denies_nested_in_worktree_outside_repo(tmp_path: Path) -> None:
    """integrate() must deny nested repo inside candidate worktree
    when worktree is outside canonical repo."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree_with_nested_in_worktree(tmp_path, repo, "REQ-REPO-005-WT-1")

    # Edit a file inside the nested Git repository inside the worktree
    path = worktree / "nested_in_wt" / "unauthorized.txt"
    path.write_text("unauthorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        # No grant for nested_in_wt
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-WT-1", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    # Should be denied with nested_authority_denied status
    assert result["status"] == "nested_authority_denied"
    assert result["commit_sha"] is None
    assert "Nested repository identity authority denied" in result["message"]
    assert "nested_in_wt" in result["message"]
    assert "primary_target_authority_not_transitive_to_nested_identity" in result["message"]
    assert "nested_identity_explicit_grant_missing_or_mismatched" in result["message"]


def test_integrate_allows_nested_in_worktree_with_grant(tmp_path: Path) -> None:
    """integrate() must allow nested repo inside candidate worktree
    when exact grant is given."""
    repo, nested = _repository_with_nested_git(tmp_path)
    worktree = _worktree_with_nested_in_worktree(tmp_path, repo, "REQ-REPO-005-WT-2")

    # Edit a file inside the nested Git repository inside the worktree
    path = worktree / "nested_in_wt" / "authorized.txt"
    path.write_text("authorized edit\n", encoding="utf-8")

    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=tmp_path / "requirements.md",
        require_spec_read_only=False,
        agents={},
        project_name="test-project",
        project_authority_grants={"nested_in_wt": "nested_git_repository"},
    )

    state = _base_state(tmp_path, repo, worktree, "REQ-REPO-005-WT-2", cfg)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.push"),
    ):
        result = integrate(state)

    # Should succeed (pushed status) because grant matches exactly
    assert result["status"] == "pushed"
    assert result["commit_sha"] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])