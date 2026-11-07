"""REQ-REPO-004: candidate-comparison base revision and revalidation on base movement.

These tests pin that ``review`` records the exact canonical base revision the candidate
was compared against, and that ``integrate`` detects movement of that revision before
commit/push, skips stale integration, appends durable evidence, and routes deterministically
to replan-based revalidation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from converge_orchestrator.git import create_worktree, current_head
from converge_orchestrator.models import (
    ComplianceEntry,
    ComplianceSnapshot,
    GateResult,
    ProjectConfig,
    Requirement,
    RequirementStatus,
    TaskEnvelope,
)
from converge_orchestrator.risk import RiskReport
from converge_orchestrator.spec import sha256_file
from converge_orchestrator.workflow import integrate, review, route_after_integrate

_RECORDED_BASE_KEY = "recorded_canonical_base_revision"


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


def _origin_and_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Return a cloned repo and its bare origin, both on ``main`` with one commit."""
    origin = tmp_path / "origin.git"
    _run_git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    repo = tmp_path / "repo"
    _run_git(tmp_path, "clone", str(origin), str(repo))
    _run_git(repo, "config", "user.email", "unit@example.invalid")
    _run_git(repo, "config", "user.name", "Unit")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "baseline")
    _run_git(repo, "push", "-u", "origin", "main")
    return repo, origin


def _config(repo: Path, tmp_path: Path) -> ProjectConfig:
    requirements = tmp_path / "requirements.md"
    requirements.write_text(
        "REQ-REPO-004 The canonical base revision used for planning and candidate "
        "comparison SHALL be recorded exactly when one exists.\n",
        encoding="utf-8",
    )
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        agents={},
        github_repo=None,
        require_spec_read_only=False,
    )


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="REQ-REPO-004-0038",
        requirement_ids=["REQ-REPO-004"],
        title="Record canonical base revision",
        objective="Record the base revision and guard against base movement.",
        allowed_paths=["src/**", "tests/**"],
        change_kind="behavior",
        tdd={
            "mode": "required",
            "test_paths": ["tests/**"],
            "test_gate": None,
            "expected_failure_pattern": "test_base_movement",
            "rationale": "RED phase for REQ-REPO-004 base movement guard.",
        },
    )


def _compliance() -> ComplianceSnapshot:
    return ComplianceSnapshot(
        entries={
            "REQ-REPO-004": ComplianceEntry(
                requirement_id="REQ-REPO-004",
                status=RequirementStatus.UNVERIFIED,
                evidence=[],
            ),
        },
        mandatory_regressions=0,
    )


def _worktree(repo: Path, tmp_path: Path) -> Path:
    worktree, _branch = create_worktree(
        repo, tmp_path / "worktrees", "REQ-REPO-004-0038", "main"
    )
    return worktree


def _review_state(
    tmp_path: Path,
    repo: Path,
    worktree: Path,
    config: ProjectConfig,
) -> dict:
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-req-repo-004",
        "requirements_hash": sha256_file(config.requirements_path),
        "requirements": [
            Requirement(
                id="REQ-REPO-004",
                statement="Record the canonical base revision.",
                source="requirements.md:L1",
                severity="mandatory",
            ).model_dump(mode="json")
        ],
        "task": _task().model_dump(mode="json"),
        "worktree": str(worktree),
        "compliance": _compliance().model_dump(mode="json"),
        "risk_flags": [],
        "approved_risk_flags": [],
        "risk_report": None,
        "risk_fingerprint": None,
        "review_result": None,
    }


def _integrate_state(
    tmp_path: Path,
    repo: Path,
    worktree: Path,
    config: ProjectConfig,
    recorded_base: str | None,
) -> dict:
    gate = GateResult(
        name="target-test-suite",
        ok=True,
        required=True,
        returncode=0,
        output="pass",
    )
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-req-repo-004",
        "requirements_hash": sha256_file(config.requirements_path),
        "task": _task().model_dump(mode="json"),
        "worktree": str(worktree),
        "branch": "converge/req-repo-004-0038",
        "quality_results": [gate.model_dump(mode="json")],
        "review_result": {"verdict": "pass", "findings": []},
        "compliance": _compliance().model_dump(mode="json"),
        _RECORDED_BASE_KEY: recorded_base,
        "replan_attempts": 0,
    }


def test_canonical_base_revision_resolves_to_commit_or_none(tmp_path: Path) -> None:
    """The canonical base resolves to the ref's commit when present and None when absent."""
    from converge_orchestrator import git as git_mod

    if not hasattr(git_mod, "resolve_canonical_base_revision"):
        pytest.fail(
            "git.resolve_canonical_base_revision is required by REQ-REPO-004"
        )

    repo, _origin = _origin_and_repo(tmp_path)
    committed_base = current_head(repo)
    assert (
        git_mod.resolve_canonical_base_revision(repo, "main") == committed_base
    ), "canonical base should resolve to the existing base commit"

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _run_git(unborn, "init", "-b", "main")
    assert (
        git_mod.resolve_canonical_base_revision(unborn, "main") is None
    ), "canonical base should be None when the base ref does not exist"


def test_review_records_canonical_base_revision_when_base_ref_exists(
    tmp_path: Path,
) -> None:
    """Review writes the recorded canonical base revision into workflow state."""
    repo, _origin = _origin_and_repo(tmp_path)
    config = _config(repo, tmp_path)
    worktree = _worktree(repo, tmp_path)
    state = _review_state(tmp_path, repo, worktree, config)
    store = SimpleNamespace(
        write_json=Mock(),
        write_text=Mock(),
        append_event=Mock(),
    )
    expected_base = current_head(repo)

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=config),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch(
            "converge_orchestrator.workflow.classify_repository_risk",
            return_value=RiskReport(),
        ),
        patch(
            "converge_orchestrator.opencode.OpenCodeAdapter.invoke"
        ) as invoke,
    ):
        invoke.return_value = SimpleNamespace(
            ok=True,
            output=json.dumps({"verdict": "pass", "findings": []}),
            context=None,
        )
        result = review(state)  # type: ignore[arg-type]

    assert (
        result.get(_RECORDED_BASE_KEY) == expected_base
    ), "review must record the canonical base revision when the base ref exists"

    # The exact evidence shape is left to the implementation; any durable evidence
    # event or artifact carrying the canonical base revision satisfies the contract.
    event_bases = [
        call.args[2].get("canonical_base_revision")
        for call in store.append_event.call_args_list
        if len(call.args) > 2 and isinstance(call.args[2], dict)
    ]
    artifact_bases = [
        call.args[2]
        for call in store.write_json.call_args_list
        if call.args[1] == "review.json"
    ]
    written_bases = [
        data.get("canonical_base_revision")
        for data in artifact_bases
        if isinstance(data, dict)
    ]
    assert expected_base in (
        *event_bases,
        *written_bases,
    ), "review evidence must carry the canonical base revision"


def test_integrate_with_matching_base_commit_pushes(tmp_path: Path) -> None:
    """When the recorded base matches the current base, integrate commits and pushes."""
    repo, _origin = _origin_and_repo(tmp_path)
    config = _config(repo, tmp_path)
    worktree = _worktree(repo, tmp_path)
    base = current_head(repo)
    (worktree / "feature.txt").write_text("candidate\n", encoding="utf-8")
    state = _integrate_state(tmp_path, repo, worktree, config, base)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=config),
        patch(
            "converge_orchestrator.workflow.sha256_file",
            return_value=state["requirements_hash"],
        ),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.commit_all") as commit_all,
        patch("converge_orchestrator.workflow.push") as push,
    ):
        commit_all.return_value = "candidate-sha"
        result = integrate(state)  # type: ignore[arg-type]

    commit_all.assert_called_once_with(worktree, "feat: Record canonical base revision")
    push.assert_called_once_with(worktree, state["branch"])
    assert result["commit_sha"] == "candidate-sha"
    assert result["status"] == "pushed"


def test_base_movement_before_integration_requires_revalidation_instead_of_stale_push(
    tmp_path: Path,
) -> None:
    """Base movement between review and integrate must skip the stale push and replan."""
    repo, _origin = _origin_and_repo(tmp_path)
    config = _config(repo, tmp_path)
    worktree = _worktree(repo, tmp_path)
    original_base = current_head(repo)

    # Candidate is ready in its worktree.
    (worktree / "feature.txt").write_text("candidate\n", encoding="utf-8")

    # Simulate external base movement: origin/main advances while the candidate waited.
    _run_git(repo, "checkout", "main")
    (repo / "base_marker.txt").write_text("new base content\n", encoding="utf-8")
    _run_git(repo, "add", "base_marker.txt")
    _run_git(repo, "commit", "-m", "base moved")
    _run_git(repo, "push", "origin", "main")

    state = _integrate_state(tmp_path, repo, worktree, config, original_base)
    store = SimpleNamespace(append_event=Mock())

    with (
        patch("converge_orchestrator.workflow.load_config", return_value=config),
        patch(
            "converge_orchestrator.workflow.sha256_file",
            return_value=state["requirements_hash"],
        ),
        patch("converge_orchestrator.workflow._evidence", return_value=store),
        patch("converge_orchestrator.workflow._write_compliance"),
        patch("converge_orchestrator.workflow.commit_all") as commit_all,
        patch("converge_orchestrator.workflow.push") as push,
    ):
        result = integrate(state)  # type: ignore[arg-type]

    commit_all.assert_not_called()
    push.assert_not_called()
    assert result["commit_sha"] is None
    assert result["status"] == "base_moved"

    events = [call.args for call in store.append_event.call_args_list]
    base_moved_events = [args for args in events if args[1] == "base_moved"]
    assert len(base_moved_events) == 1
    payload = base_moved_events[0][2]
    assert payload["recorded_canonical_base_revision"] == original_base
    assert payload["current_canonical_base_revision"] != original_base

    # Deterministic revalidation path: base movement must route to replan, not push.
    with patch("converge_orchestrator.workflow.load_config", return_value=config):
        assert route_after_integrate(result) == "replan"  # type: ignore[arg-type]
