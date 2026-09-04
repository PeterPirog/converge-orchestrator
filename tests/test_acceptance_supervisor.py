from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from converge_orchestrator.acceptance import (
    AcceptanceCheck,
    ExternalAcceptanceReport,
    ExternalSupervisorEvidence,
)
from converge_orchestrator.acceptance_supervisor import (
    AcceptanceSupervisorError,
    _candidate_fingerprint,
    _run_final_audit,
    _validate_acceptance_preconditions,
    _wait_for_risk_interrupt,
)
from converge_orchestrator.models import AgentResult, ProjectConfig

_PINNED_IMAGE = "ghcr.io/example/runtime@sha256:" + "a" * 64
_REVIEW_ROLES = ["correctness_reviewer", "architecture_reviewer", "security_reviewer"]


def _config(tmp_path: Path, **overrides) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text(
        "ARCH-001 First mandatory requirement.\nARCH-002 Second mandatory requirement.\n",
        encoding="utf-8",
    )
    payload = {
        "project_name": "external-acceptance",
        "repo_path": repo,
        "requirements_path": requirements,
        "state_dir": tmp_path / "state",
        "require_spec_read_only": True,
        "github_repo": "example/target",
        "auto_merge": True,
        "auto_discover_quality": False,
        "quality_gates": [{"name": "tests", "command": ["python", "-m", "pytest"]}],
        "sandbox": {
            "mode": "container",
            "image": _PINNED_IMAGE,
            "agent_network": "converge-ai",
            "quality_network": "none",
        },
        "agents": {
            role: {"agent": f"converge-{role.replace('_', '-')}"}
            for role in _REVIEW_ROLES
        },
        "review_roles": _REVIEW_ROLES,
    }
    payload.update(overrides)
    return ProjectConfig(**payload)


def _supervisor(run_id: str = "run-1") -> ExternalSupervisorEvidence:
    return ExternalSupervisorEvidence.model_validate(
        {
            "run_id": run_id,
            "target_repository": "example/target",
            "restart": {
                "before_pid": 10,
                "after_pid": 20,
                "automatic_recovery_observed": True,
            },
            "exceptional_hitl": {
                "kind": "risk_policy",
                "deliberately_injected": True,
                "action": "approve",
                "no_manual_code_edit": True,
            },
            "final_independent_checks": {
                "requirements": "reject",
                "architecture": "reject",
                "compatibility": "reject",
                "security": "reject",
                "evidence": "reject",
            },
        }
    )


def test_acceptance_preconditions_require_external_automerge_pinned_reviewed_project(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)

    with patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True):
        _validate_acceptance_preconditions(cfg)


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"github_repo": "PeterPirog/converge-orchestrator"}, "outside Converge"),
        ({"auto_merge": False}, "auto_merge"),
        ({"review_roles": ["correctness_reviewer"]}, "missing required independent review roles"),
    ],
)
def test_acceptance_preconditions_fail_closed(
    tmp_path: Path,
    override: dict,
    expected: str,
) -> None:
    cfg = _config(tmp_path, **override)

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        pytest.raises(AcceptanceSupervisorError, match=expected),
    ):
        _validate_acceptance_preconditions(cfg)


def test_candidate_fingerprint_is_bound_to_checkpointed_diff(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    patch_text = "diff --git a/a.py b/a.py\n+change\n"
    import hashlib

    expected = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    observer = SimpleNamespace(
        status=lambda _run_id: {
            "values": {
                "worktree": str(tmp_path / "worktree"),
                "risk_fingerprint": expected,
            }
        }
    )

    with (
        patch(
            "converge_orchestrator.acceptance_supervisor._pinned_config_for_run",
            return_value=cfg,
        ),
        patch("converge_orchestrator.acceptance_supervisor.diff", return_value=patch_text),
    ):
        assert _candidate_fingerprint(observer, "run-1") == expected

    with (
        patch(
            "converge_orchestrator.acceptance_supervisor._pinned_config_for_run",
            return_value=cfg,
        ),
        patch("converge_orchestrator.acceptance_supervisor.diff", return_value="different"),
        pytest.raises(AcceptanceSupervisorError, match="candidate changed"),
    ):
        _candidate_fingerprint(observer, "run-1")


def test_final_audit_uses_fresh_read_only_review_lanes_and_deterministic_evidence(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    calls: list[str] = []

    class FakeAdapter:
        def __init__(self, _cfg: ProjectConfig):
            pass

        def invoke(self, role: str, prompt: str, _cwd: Path) -> AgentResult:
            calls.append(role)
            assert "FINAL read-only external acceptance audit" in prompt
            return AgentResult(
                role=role,
                ok=True,
                output='{"verdict":"pass","findings":[]}',
            )

    deterministic = ExternalAcceptanceReport(
        run_id="run-1",
        project_id="external-acceptance",
        target_repository="example/target",
        ready=True,
        merged_task_ids=["ARCH-001-1", "ARCH-002-1"],
        checks=[AcceptanceCheck(name="event_stream", ok=True, evidence="ok")],
    )
    with (
        patch("converge_orchestrator.acceptance_supervisor.OpenCodeAdapter", FakeAdapter),
        patch(
            "converge_orchestrator.acceptance_supervisor.evaluate_external_acceptance",
            return_value=deterministic,
        ),
    ):
        checks, audit = _run_final_audit(cfg, "run-1", {}, _supervisor())

    assert checks == {
        "requirements": "pass",
        "architecture": "pass",
        "compatibility": "pass",
        "security": "pass",
        "evidence": "pass",
    }
    assert calls == [
        "architecture_reviewer",
        "architecture_reviewer",
        "correctness_reviewer",
        "security_reviewer",
    ]
    assert audit.deterministic_evidence_ok is True


def test_risk_interrupt_must_match_predeclared_injected_flag() -> None:
    api = SimpleNamespace(base_url="http://127.0.0.1:1", token="token")
    response = {
        "interrupt": {
            "kind": "risk_policy",
            "risk_flags": ["critical_auth_redesign"],
        }
    }
    with (
        patch("converge_orchestrator.acceptance_supervisor._api_json", return_value=response),
        pytest.raises(AcceptanceSupervisorError, match="predeclared injected risk flag"),
    ):
        _wait_for_risk_interrupt(
            api,
            "run-1",
            "forbidden_public_api_change",
            deadline=10**12,
            poll_seconds=0.01,
        )
