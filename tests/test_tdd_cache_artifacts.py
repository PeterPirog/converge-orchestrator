"""Regression tests for gate-generated cache artifacts (V5 acceptance failure).

The external acceptance V5 run (run 9b85569e051e4c8b947b1cec9916ee94) failed closed because the
TDD baseline gate ran pytest in the candidate worktree, pytest produced untracked
``__pycache__`` directories, and ``changed_files`` counted them as worktree changes, making the
baseline unusable and triggering an unbounded replan loop that ended in a planner_failure_budget
HITL before any merge. These tests pin the corrected behavior: deterministic cache artifacts are
never candidate changes, never counted, and never committed.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.git import (
    changed_files,
    commit_all,
    diff_line_count,
    is_deterministic_cache_artifact,
)
from converge_orchestrator.models import ProjectConfig, QualityGate, TaskEnvelope
from converge_orchestrator.tdd import run_tdd_baseline, run_tdd_red


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
    _run_git(repo, "config", "user.email", "converge@example.invalid")
    _run_git(repo, "config", "user.name", "Converge Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    return repo


def _config(tmp_path: Path, repo: Path) -> ProjectConfig:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("System must expose the requested behavior.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        base_branch="main",
        require_spec_read_only=False,
        agents={},
        auto_discover_quality=False,
        quality_gates=[
            QualityGate(
                name="unit-test",
                command=["python", "-m", "pytest", "-q"],
                timeout_seconds=30,
            )
        ],
    )


def _behavior_task() -> TaskEnvelope:
    return TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Add behavior",
        objective="Expose the required behavior",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd={
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": "unit-test",
            "expected_failure_pattern": "NEW_RULE_MISSING",
            "rationale": "Observable behavior changes require a failing test first.",
        },
    )


def _create_gate_caches(repo: Path) -> None:
    """Create the exact untracked artifacts a pytest quality-gate run leaves behind."""
    (repo / "shared_tools" / "__pycache__").mkdir(parents=True)
    (repo / "shared_tools" / "__pycache__" / "fake_terminal.cpython-313.pyc").write_bytes(
        b"\x00compiled"
    )
    (repo / "tests" / "__pycache__").mkdir(parents=True)
    (repo / "tests" / "__pycache__" / "test_rule.cpython-313.pyc").write_bytes(b"\x00compiled")
    pytest_cache = repo / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "nodeids").write_text("[]", encoding="utf-8")


def test_is_deterministic_cache_artifact_matches_gate_byproducts() -> None:
    assert is_deterministic_cache_artifact("shared_tools/__pycache__/")
    assert is_deterministic_cache_artifact("shared_tools/__pycache__/mod.cpython-313.pyc")
    assert is_deterministic_cache_artifact("tests/__pycache__/")
    assert is_deterministic_cache_artifact(".pytest_cache/v/cache/nodeids")
    assert is_deterministic_cache_artifact(".mypy_cache/1/data.json")
    assert is_deterministic_cache_artifact("shared_tools/module.pyo")
    assert is_deterministic_cache_artifact("shared_tools\\__pycache__\\mod.pyc")
    assert not is_deterministic_cache_artifact("shared_tools/fake_terminal.py")
    assert not is_deterministic_cache_artifact("tests/test_fake_terminal.py")
    assert not is_deterministic_cache_artifact("README.md")


def test_changed_files_ignores_gate_generated_cache_artifacts(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _create_gate_caches(repo)
    (repo / "notes.txt").write_text("real candidate change\n", encoding="utf-8")

    assert changed_files(repo, "main") == ["notes.txt"]


def test_diff_line_count_ignores_gate_generated_cache_artifacts(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _create_gate_caches(repo)
    (repo / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")

    assert diff_line_count(repo, "main") == 2


def test_tdd_baseline_stays_usable_when_gate_leaves_python_caches(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    cfg = _config(tmp_path, repo)
    task = _behavior_task()
    _create_gate_caches(repo)

    with patch(
        "converge_orchestrator.tdd.ExecutionSandbox.run",
        return_value=types.SimpleNamespace(returncode=0, stdout="2 passed"),
    ):
        baseline = run_tdd_baseline(cfg, repo, task)

    assert baseline.ok
    payload = json.loads(baseline.output)
    assert payload["gate_ok"] is True
    assert payload["changed_files_after_baseline"] == []
    assert payload["usable"] is True


def test_tdd_red_accepts_test_only_diff_alongside_gate_caches(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    cfg = _config(tmp_path, repo)
    task = _behavior_task()
    _create_gate_caches(repo)
    sandbox_patch_target = "converge_orchestrator.tdd.ExecutionSandbox.run"

    with patch(
        sandbox_patch_target,
        return_value=types.SimpleNamespace(returncode=0, stdout="2 passed"),
    ):
        baseline = run_tdd_baseline(cfg, repo, task)
    assert baseline.ok

    # The RED test is written after the baseline, exactly as the builder phase does.
    test_file = repo / "tests" / "test_rule.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n",
        encoding="utf-8",
    )

    with patch(
        sandbox_patch_target,
        return_value=types.SimpleNamespace(
            returncode=1, stdout="ImportError: cannot import name; AssertionError: NEW_RULE_MISSING"
        ),
    ):
        red = run_tdd_red(cfg, repo, task, baseline)

    assert red.ok
    payload = json.loads(red.output)
    assert payload["test_paths_ok"] is True
    assert payload["deterministic_test_artifacts_ok"] is True
    assert payload["task_scope_ok"] is True
    assert set(payload["changed_files"]) == {"tests/test_rule.py"}
    assert set(payload["red_test_sha256"]) == {"tests/test_rule.py"}


def test_commit_all_never_commits_gate_generated_caches(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _create_gate_caches(repo)
    (repo / "README.md").write_text("candidate change\n", encoding="utf-8")
    (repo / "candidate.py").write_text("value = 1\n", encoding="utf-8")

    commit = commit_all(repo, "feat: candidate")

    assert commit is not None
    committed = _run_git(repo, "show", "--name-only", "--format=")
    assert set(committed.splitlines()) == {"README.md", "candidate.py"}
    assert not (repo / "shared_tools" / "__pycache__").exists()
    assert not (repo / ".pytest_cache").exists()
