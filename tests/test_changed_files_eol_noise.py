"""Regression tests for EOL-phantom status entries and stderr noise in path enumeration.

The external acceptance V6 run (run e9cf271fb7924d62a5cbdab2906bc9b4) failed closed at the
tdd_human gate although the builder produced a valid test-only RED diff. changed_files()
counted three kinds of git-status noise as candidate changes: (a) tracked files the sandbox
rewrote byte-identically with LF endings while core.autocrlf=true made git status report
them modified with an empty content diff (the checkout-time stat entry records the CRLF
size, so the stat fast path never heals), (b) CRLF conversion warnings that run() merges
into stdout, parsed verbatim as file paths, and (c) a first porcelain line whose leading
status column was stripped by _git()'s whole-output strip(), so the positional column parse
mangled .github/workflows/acceptance-ci.yml into github/... . The TDD red gate therefore
rejected a test-only diff three times and exhausted the failure budget. These tests pin
content-based, stderr-free path enumeration.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.git import changed_files, diff_line_count
from converge_orchestrator.models import ProjectConfig, QualityGate, TaskEnvelope
from converge_orchestrator.tdd import run_tdd_baseline, run_tdd_red

README_BLOB = b"baseline\n"
WORKFLOW_BLOB = b"name: ci\non: push\n"


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_stdout(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
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
    # byte-identical LF rewrite of a tracked file makes git status report a phantom
    # modification while the content diff stays empty.
    _run_git(repo, "config", "core.autocrlf", "true")
    (repo / "README.md").write_bytes(README_BLOB)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "acceptance-ci.yml").write_bytes(WORKFLOW_BLOB)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    # Materialize smudged (CRLF) working copies with checkout-time stat entries, exactly like
    # a fresh worktree checkout under core.autocrlf=true, so the sandbox-style LF rewrites
    # below produce phantom status modifications with empty content diffs.
    (repo / "README.md").unlink()
    (repo / ".github" / "workflows" / "acceptance-ci.yml").unlink()
    _run_git(repo, "checkout", "--", ".")
    return repo


def _phantom_rewrite(repo: Path, relative: str, blob: bytes) -> None:
    """Write the committed LF blob over the smudged working copy, like the sandbox tooling."""

    (repo / relative).write_bytes(blob)


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


def test_eol_identical_rewrite_is_status_modified_with_empty_content_diff(
    tmp_path: Path,
) -> None:
    """Pin the environment precondition: status says modified, the content diff says nothing."""
    repo = _repository(tmp_path)
    _phantom_rewrite(repo, "README.md", README_BLOB)

    status = _git_stdout(repo, "status", "--porcelain", "-uall")
    assert " M README.md\n" in status
    assert _git_stdout(repo, "diff", "--name-only", "HEAD") == ""


def test_changed_files_ignores_eol_only_status_modifications(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _phantom_rewrite(repo, "README.md", README_BLOB)

    assert changed_files(repo, "main") == []


def test_changed_files_reports_content_changes_with_verbatim_dot_paths(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    # The byte-identical rewrite below is a phantom; the workflow rewrite is a real content
    # change. In `git status` output the dot-leading path is the first porcelain line, whose
    # leading status column the old whole-output strip() removed, so the positional parse
    # mangled it into github/... . Enumeration must keep the dot path verbatim, skip the
    # phantom, and never emit stderr warnings as paths.
    _phantom_rewrite(repo, "README.md", README_BLOB)
    (repo / ".github" / "workflows" / "acceptance-ci.yml").write_bytes(
        b"name: ci\non: push\nbranches:\n  - main\n"
    )
    status = _git_stdout(repo, "status", "--porcelain", "-uall")
    assert status.startswith(" M .github/workflows/acceptance-ci.yml\n")
    assert " M README.md\n" in status

    assert changed_files(repo, "main") == [".github/workflows/acceptance-ci.yml"]


def test_changed_files_enumerates_untracked_files_individually(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    # A fully untracked directory tree collapses to one line without per-file enumeration.
    (repo / "newpkg" / "__pycache__").mkdir(parents=True)
    (repo / "newpkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00compiled")
    (repo / "newpkg" / "mod.py").write_bytes(b"value = 1\n")

    assert changed_files(repo, "main") == ["newpkg/mod.py"]


def test_diff_line_count_ignores_eol_only_status_modifications(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _phantom_rewrite(repo, "README.md", README_BLOB)
    (repo / "notes.txt").write_bytes(b"one\ntwo\n")

    assert diff_line_count(repo, "main") == 2


def test_tdd_baseline_and_red_survive_eol_phantoms_with_valid_test_only_diff(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    cfg = _config(tmp_path, repo)
    task = _behavior_task()
    # Phantom rewrites of files the builder merely read, mirroring the V6 worktree state.
    _phantom_rewrite(repo, "README.md", README_BLOB)
    _phantom_rewrite(repo, ".github/workflows/acceptance-ci.yml", WORKFLOW_BLOB)
    sandbox_patch_target = "converge_orchestrator.tdd.ExecutionSandbox.run"

    with patch(
        sandbox_patch_target,
        return_value=types.SimpleNamespace(returncode=0, stdout="2 passed"),
    ):
        baseline = run_tdd_baseline(cfg, repo, task)

    assert baseline.ok
    payload = json.loads(baseline.output)
    assert payload["changed_files_after_baseline"] == []
    assert payload["usable"] is True

    # The RED test is written after the baseline, exactly as the builder phase does, while
    # the phantom rewrites remain in the worktree.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_rule.py").write_text(
        "def test_new_rule():\n    assert False, 'NEW_RULE_MISSING'\n",
        encoding="utf-8",
    )

    with patch(
        sandbox_patch_target,
        return_value=types.SimpleNamespace(
            returncode=1,
            stdout="ImportError: cannot import name; AssertionError: NEW_RULE_MISSING",
        ),
    ):
        red = run_tdd_red(cfg, repo, task, baseline)

    assert red.ok
    red_payload = json.loads(red.output)
    assert red_payload["changed_files"] == ["tests/test_rule.py"]
    assert red_payload["test_paths_ok"] is True
    assert red_payload["deterministic_test_artifacts_ok"] is True
    assert red_payload["task_scope_ok"] is True
    assert set(red_payload["red_test_sha256"]) == {"tests/test_rule.py"}