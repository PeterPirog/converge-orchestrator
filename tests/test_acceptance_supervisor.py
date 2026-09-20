from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    supervise_external_acceptance,
)
from converge_orchestrator.models import AgentResult, ProjectConfig
from converge_orchestrator.persistence import configured_control_db_path

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
                "expected_risk_flag": "forbidden_public_api_change",
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
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path)
    # Set explicit control DB for SQLite mode acceptance
    explicit_db = tmp_path / "control.sqlite"
    explicit_db.touch()
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

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
    tmp_path: Path, monkeypatch, override: dict, expected: str
) -> None:
    cfg = _config(tmp_path, **override)
    # Set explicit control DB for SQLite mode acceptance
    explicit_db = tmp_path / "control.sqlite"
    explicit_db.touch()
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)

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
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        _wait_for_risk_interrupt(
            api,
            "run-1",
            "forbidden_public_api_change",
            deadline=10**12,
            poll_seconds=0.01,
        )

    assert exc_info.value.failure_kind == "risk_flag_mismatch"
    assert exc_info.value.interrupt_kind == "risk_policy"


def test_risk_interrupt_structures_unexpected_human_interrupt() -> None:
    api = SimpleNamespace(base_url="http://127.0.0.1:1", token="token")
    response = {"interrupt": {"kind": "planner_failure_budget", "risk_flags": []}}
    with (
        patch("converge_orchestrator.acceptance_supervisor._api_json", return_value=response),
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        _wait_for_risk_interrupt(
            api,
            "run-1",
            "forbidden_public_api_change",
            deadline=10**12,
            poll_seconds=0.01,
        )

    assert exc_info.value.failure_kind == "unexpected_human_interrupt"
    assert exc_info.value.interrupt_kind == "planner_failure_budget"


def test_supervise_writes_failure_record_on_unexpected_human_interrupt(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    output_path = tmp_path / "acceptance-supervisor.json"
    api_calls: list[tuple[str, str]] = []

    def fake_api_json(_base_url, _token, method, path, payload=None, **_kwargs):
        api_calls.append((method, path))
        if path == "/projects":
            return {}
        if path.endswith("/run"):
            return {"id": "run-1"}
        return {}

    pids = iter([111, 222])

    def fake_start_api(_config, _run_id_hint):
        return SimpleNamespace(
            process=SimpleNamespace(pid=next(pids), poll=lambda: 0),
            stop=lambda: None,
            base_url="http://127.0.0.1:1",
            token="t",
        )

    decision = Mock(return_value="approve")
    failure = AcceptanceSupervisorError(
        "unexpected human interrupt during acceptance: planner_failure_budget",
        failure_kind="unexpected_human_interrupt",
        interrupt_kind="planner_failure_budget",
    )

    with (
        patch("converge_orchestrator.acceptance_supervisor.load_config", return_value=cfg),
        patch("converge_orchestrator.acceptance_supervisor._validate_acceptance_preconditions"),
        patch(
            "converge_orchestrator.acceptance_supervisor._project_and_unfinished_run",
            return_value=(None, None),
        ),
        patch(
            "converge_orchestrator.acceptance_supervisor._start_api",
            side_effect=fake_start_api,
        ),
        patch("converge_orchestrator.acceptance_supervisor._api_json", side_effect=fake_api_json),
        patch(
            "converge_orchestrator.acceptance_supervisor._pinned_config_for_run",
            return_value=cfg,
        ),
        patch(
            "converge_orchestrator.acceptance_supervisor._observer",
            return_value=SimpleNamespace(),
        ),
        patch("converge_orchestrator.acceptance_supervisor._wait_for_first_merge"),
        patch("converge_orchestrator.acceptance_supervisor._events", return_value=[]),
        patch("converge_orchestrator.acceptance_supervisor._wait_for_automatic_recovery"),
        patch(
            "converge_orchestrator.acceptance_supervisor._wait_for_risk_interrupt",
            side_effect=failure,
        ),
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        supervise_external_acceptance(
            tmp_path / "converge.yaml",
            project_id="external-acceptance",
            expected_risk_flag="forbidden_public_api_change",
            output_path=output_path,
            decision_provider=decision,
            poll_seconds=0.01,
        )

    assert exc_info.value is failure
    decision.assert_not_called()
    assert ("POST", "/runs/run-1/decision") not in api_calls
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["project_id"] == "external-acceptance"
    assert payload["target_repository"] == "example/target"
    assert payload["expected_risk_flag"] == "forbidden_public_api_change"
    assert payload["failure_kind"] == "unexpected_human_interrupt"
    assert payload["interrupt_kind"] == "planner_failure_budget"
    assert "planner_failure_budget" in payload["detail"]
    assert payload["progress"]["restart_done"] is True
    assert payload["progress"]["automatic_recovery_observed"] is True
    assert payload["progress"]["hitl_done"] is False
    evidence_copy = (
        cfg.state_dir / "evidence" / "run-1" / "external-acceptance-failure.json"
    )
    assert json.loads(evidence_copy.read_text(encoding="utf-8")) == payload


def test_acceptance_preconditions_require_explicit_control_db_in_sqlite_mode(
    tmp_path: Path, monkeypatch
) -> None:
    """External acceptance in SQLite mode must fail closed if
    CONVERGE_CONTROL_DB is not explicitly set."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        _validate_acceptance_preconditions(cfg)

    assert exc_info.value.failure_kind == "control_db_not_explicit"
    assert "CONVERGE_CONTROL_DB" in str(exc_info.value)
    assert "explicit" in str(exc_info.value).lower()


def test_acceptance_preconditions_control_db_failure_independent_of_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """Control DB identity failure must be independent of current working directory."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)

    # Test from two different temporary working directories
    for _ in range(2):
        with (
            patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
            pytest.raises(AcceptanceSupervisorError, match="CONVERGE_CONTROL_DB"),
        ):
            _validate_acceptance_preconditions(cfg)


def test_acceptance_preconditions_explicit_control_db_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit CONVERGE_CONTROL_DB pointing to a stable path should succeed."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    explicit_db = tmp_path / "explicit-control.sqlite"
    explicit_db.touch()  # Create the file
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))

    with patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True):
        _validate_acceptance_preconditions(cfg)


def test_acceptance_preconditions_postgres_does_not_require_control_db(
    tmp_path: Path, monkeypatch
) -> None:
    """PostgreSQL acceptance should not require CONVERGE_CONTROL_DB
    when CONVERGE_DATABASE_URL is set."""
    cfg = _config(tmp_path)
    monkeypatch.setenv("CONVERGE_DATABASE_URL", "postgresql://user:pass@db/converge")
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        patch("converge_orchestrator.persistence.PostgresControlRegistry"),
        patch("converge_orchestrator.persistence._verify_postgres_checkpoint_schema"),
    ):
        _validate_acceptance_preconditions(cfg)


def test_acceptance_preconditions_empty_control_db_value_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Empty CONVERGE_CONTROL_DB value should fail closed."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONVERGE_CONTROL_DB", "  ")  # whitespace only

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        pytest.raises(AcceptanceSupervisorError, match="CONVERGE_CONTROL_DB"),
    ):
        _validate_acceptance_preconditions(cfg)


def test_configured_control_db_path_returns_absolute_path_when_env_set(
    tmp_path: Path, monkeypatch
) -> None:
    """configured_control_db_path should return absolute path when CONVERGE_CONTROL_DB is set."""
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)
    explicit_db = tmp_path / "control.sqlite"
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))

    result = configured_control_db_path()
    assert result.is_absolute()
    assert result == explicit_db.resolve()


def test_configured_control_db_path_resolves_relative_path(
    tmp_path: Path, monkeypatch
) -> None:
    """configured_control_db_path should resolve relative paths."""
    monkeypatch.delenv("CONVERGE_CONTROL_DB", raising=False)
    explicit_db = "relative/control.sqlite"
    monkeypatch.setenv("CONVERGE_CONTROL_DB", explicit_db)

    result = configured_control_db_path()
    assert result.is_absolute()
    assert result.name == "control.sqlite"


def test_acceptance_preconditions_reject_relative_control_db_path(
    tmp_path: Path, monkeypatch
) -> None:
    """External acceptance must reject relative CONVERGE_CONTROL_DB paths."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONVERGE_CONTROL_DB", "relative/control.sqlite")

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        _validate_acceptance_preconditions(cfg)

    assert exc_info.value.failure_kind == "control_db_not_explicit"
    assert "CONVERGE_CONTROL_DB" in str(exc_info.value)


def test_acceptance_preconditions_relative_path_rejected_across_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """Relative CONVERGE_CONTROL_DB must be rejected regardless of CWD.

    This directly represents the V23 incident: same relative path from
    different CWDs would resolve to different control registries.
    """
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONVERGE_CONTROL_DB", "relative/control.sqlite")

    # Create two distinct temporary working directories
    cwd_a = tmp_path / "cwd_a"
    cwd_b = tmp_path / "cwd_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    original_cwd = Path.cwd()
    try:
        for cwd in (cwd_a, cwd_b):
            os.chdir(cwd)
            with patch(
                "converge_orchestrator.acceptance_supervisor.is_read_only",
                return_value=True,
            ), pytest.raises(AcceptanceSupervisorError) as exc_info:
                _validate_acceptance_preconditions(cfg)

            assert exc_info.value.failure_kind == "control_db_not_explicit"
            assert "CONVERGE_CONTROL_DB" in str(exc_info.value)
    finally:
        os.chdir(original_cwd)


def test_acceptance_preconditions_absolute_control_db_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit absolute CONVERGE_CONTROL_DB should succeed."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    explicit_db = tmp_path / "explicit-control.sqlite"
    explicit_db.touch()
    monkeypatch.setenv("CONVERGE_CONTROL_DB", str(explicit_db))

    with patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True):
        _validate_acceptance_preconditions(cfg)


def test_acceptance_preconditions_whitespace_control_db_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Whitespace-only CONVERGE_CONTROL_DB should fail."""
    cfg = _config(tmp_path)
    monkeypatch.delenv("CONVERGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONVERGE_CONTROL_DB", "  \t\n  ")

    with (
        patch("converge_orchestrator.acceptance_supervisor.is_read_only", return_value=True),
        pytest.raises(AcceptanceSupervisorError) as exc_info,
    ):
        _validate_acceptance_preconditions(cfg)

    assert exc_info.value.failure_kind == "control_db_not_explicit"
    assert "CONVERGE_CONTROL_DB" in str(exc_info.value)
