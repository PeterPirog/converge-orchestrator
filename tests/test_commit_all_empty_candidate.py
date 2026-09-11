"""Regression tests for commit_all empty-candidate semantics (external acceptance V15).

The external acceptance V15 run (run 88c5862fecc04ee0be6db1ad5b466396) failed closed with raw
git stderr "nothing to commit, working tree clean". The builder correctly produced no candidate
change for an already-implemented requirement, the quality gates ran pytest inside the candidate
worktree, and commit_all then saw the resulting untracked cache artifacts in
``git status --porcelain``, deleted them, staged with ``git add -A``, and still invoked
``git commit`` with an index that contained no staged diff relative to HEAD. git exited 1 and
the GitError aborted the run before the designed ``no_changes`` terminal in workflow.integrate
was reachable. A tracked file rewritten with EOL-equivalent content produces the same crash
shape: status reports a modification with an empty content diff, so staging yields no staged
diff. These tests pin: noise-only candidate states commit nothing and return None, real
candidate changes still commit, and integrate reaches the designed ``no_changes`` outcome
instead of raising.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from converge_orchestrator.git import commit_all, create_worktree
from converge_orchestrator.models import (
    ComplianceSnapshot,
    GateResult,
    ProjectConfig,
    TaskEnvelope,
)
from converge_orchestrator.workflow import integrate

README_BLOB = b"baseline\n"


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
    # Reproduce the acceptance environment: core.autocrlf=true with no .gitattributes, so a
    # byte-identical LF rewrite of a tracked file reports a phantom status modification with
    # an empty content diff (the V15 candidate worktree state).
    _run_git(repo, "config", "core.autocrlf", "true")
    (repo / "README.md").write_bytes(README_BLOB)
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    # Materialize smudged (CRLF) working copies with checkout-time stat entries, exactly like
    # a fresh worktree checkout under core.autocrlf=true.
    (repo / "README.md").unlink()
    _run_git(repo, "checkout", "--", ".")
    return repo


def _worktree(repo: Path, tmp_path: Path) -> Path:
    worktree, _ = create_worktree(repo, tmp_path / "worktrees", "ARCH-001-1", "main")
    return worktree


def _create_gate_caches(worktree: Path) -> None:
    """Create the exact untracked artifacts a pytest quality-gate run leaves behind."""
    (worktree / "shared_tools" / "__pycache__").mkdir(parents=True)
    (worktree / "shared_tools" / "__pycache__" / "fake_terminal.cpython-313.pyc").write_bytes(
        b"\x00compiled"
    )
    (worktree / "tests" / "__pycache__").mkdir(parents=True)
    (worktree / "tests" / "__pycache__" / "test_rule.cpython-313.pyc").write_bytes(
        b"\x00compiled"
    )
    pytest_cache = worktree / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "nodeids").write_text("[]", encoding="utf-8")


def test_commit_all_ignores_untracked_cache_artifacts(tmp_path: Path) -> None:
    """Cache artifacts alone must not become a commit or a crash (V15 failure shape)."""
    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    _create_gate_caches(worktree)
    status = _git_stdout(worktree, "status", "--porcelain", "-uall")
    assert "??" in status
    assert ".pytest_cache" in status

    commit = commit_all(worktree, "feat: candidate")

    assert commit is None
    assert _git_stdout(worktree, "status", "--porcelain", "-uall") == ""
    assert _run_git(worktree, "rev-list", "--count", "origin/main..HEAD") == "0"
    assert not (worktree / ".pytest_cache").exists()
    assert not (worktree / "shared_tools" / "__pycache__").exists()


def test_commit_all_ignores_eol_equivalent_tracked_rewrites(tmp_path: Path) -> None:
    """An EOL-phantom status modification stages nothing and must not crash commit_all."""
    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    (worktree / "README.md").write_bytes(README_BLOB)
    status = _git_stdout(worktree, "status", "--porcelain")
    assert " M README.md" in status
    assert _git_stdout(worktree, "diff", "--name-only", "HEAD") == ""

    commit = commit_all(worktree, "feat: candidate")

    assert commit is None
    assert _run_git(worktree, "rev-list", "--count", "origin/main..HEAD") == "0"


def test_commit_all_commits_real_candidate_changes(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    (worktree / "feature.txt").write_text("candidate\n", encoding="utf-8")

    commit = commit_all(worktree, "feat: candidate")

    assert commit is not None
    assert commit == _run_git(worktree, "rev-parse", "HEAD")
    assert _git_stdout(worktree, "status", "--porcelain", "-uall") == ""


def test_commit_all_commits_real_changes_alongside_gate_caches(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    _create_gate_caches(worktree)
    (worktree / "feature.txt").write_text("candidate\n", encoding="utf-8")

    commit = commit_all(worktree, "feat: candidate")

    assert commit is not None
    assert commit == _run_git(worktree, "rev-parse", "HEAD")
    committed = _run_git(worktree, "show", "--name-only", "--format=")
    assert set(committed.splitlines()) == {"feature.txt"}
    assert not (worktree / "shared_tools" / "__pycache__").exists()
    assert not (worktree / ".pytest_cache").exists()
    assert _git_stdout(worktree, "status", "--porcelain", "-uall") == ""


def test_integrate_reaches_designed_no_changes_instead_of_git_error(tmp_path: Path) -> None:
    """The exact V15 candidate state must end in no_changes, not a raw GitError."""
    repo = _repository(tmp_path)
    worktree = _worktree(repo, tmp_path)
    _create_gate_caches(worktree)
    (worktree / "README.md").write_bytes(README_BLOB)

    requirements = tmp_path / "requirements.md"
    requirements.write_text("immutable\n", encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        agents={},
    )
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Verify already-implemented requirement",
        objective="Confirm the existing implementation satisfies the requirement",
        allowed_paths=["shared_tools/**", "tests/**"],
        tdd={
            "mode": "not_applicable",
            "test_paths": ["tests/**"],
            "test_gate": None,
            "expected_failure_pattern": "no failing test pattern applicable",
            "rationale": "The requirement is already satisfied; no behavior change is needed.",
        },
    )
    gate = GateResult(name="target-test-suite", ok=True, required=True, returncode=0, output="pass")
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "requirements_hash": "spec-hash",
        "task": task.model_dump(mode="json"),
        "worktree": str(worktree),
        "branch": "converge/arch-001-1",
        "quality_results": [gate.model_dump(mode="json")],
        "compliance": ComplianceSnapshot().model_dump(mode="json"),
    }
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.sha256_file", return_value="spec-hash"),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
    ):
        result = integrate(state)

    assert result["status"] == "no_changes"
    assert result["commit_sha"] is None
    assert result["message"] == "No changes produced"
    event = store.append_event.call_args.args[2]
    assert event["commit_sha"] is None
    assert event["recovered_existing_commit"] is False
