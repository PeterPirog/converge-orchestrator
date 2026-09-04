from __future__ import annotations

import json
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.budget import (
    RunBudgetExceeded,
    RunBudgetIntegrityError,
    assert_run_wall_time,
    bind_run_id,
    budget_path,
    initialize_run_budget,
    reserve_model_attempt,
    reset_run_id,
    run_budget_status,
)
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter


def _config(tmp_path: Path, *, attempts: int = 3, tokens: int = 20_000) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("ARCH-001 System must remain bounded.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        run_budget={
            "max_wall_time_seconds": 600,
            "max_model_attempts": attempts,
            "max_estimated_tokens": tokens,
        },
        model_gateway={
            "kind": "openwebui",
            "base_url": "http://127.0.0.1:3000/api",
        },
        model_profiles={
            "primary": {
                "model": "primary-model",
                "context_tokens": 32_000,
                "output_tokens": 512,
            }
        },
        agents={
            "planner": {
                "agent": "converge-planner",
                "model_profile": "primary",
                "provider_retries": 2,
            }
        },
    )


def test_documented_workflow_run_budget_is_parsed(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(
        {
            "repo_path": str(tmp_path / "repo"),
            "requirements_path": str(tmp_path / "architecture.md"),
            "agents": {},
            "workflow": {
                "run_budget": {
                    "max_wall_time_seconds": 3600,
                    "max_model_attempts": 17,
                    "max_estimated_tokens": 123_456,
                }
            },
        }
    )

    assert cfg.run_budget.max_wall_time_seconds == 3600
    assert cfg.run_budget.max_model_attempts == 17
    assert cfg.run_budget.max_estimated_tokens == 123_456


def test_model_reservations_persist_and_are_not_reset(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    started = datetime.now(UTC)
    initialize_run_budget(cfg, "run-1", started_at=started)
    reserve_model_attempt(
        cfg,
        "run-1",
        role="planner",
        model="openwebui/primary-model",
        estimated_input_tokens=100,
        output_reserve_tokens=512,
    )

    reopened = initialize_run_budget(
        cfg,
        "run-1",
        started_at=started + timedelta(hours=1),
    )
    status = run_budget_status(cfg, "run-1")

    assert reopened.started_at == started
    assert status.model_attempts_reserved == 1
    assert status.estimated_tokens_reserved == 612


def test_next_provider_attempt_is_blocked_before_counter_overrun(tmp_path: Path) -> None:
    cfg = _config(tmp_path, attempts=1)
    initialize_run_budget(cfg, "run-1", started_at=datetime.now(UTC))
    reserve_model_attempt(
        cfg,
        "run-1",
        role="planner",
        model="model",
        estimated_input_tokens=100,
        output_reserve_tokens=256,
    )

    with pytest.raises(RunBudgetExceeded):
        reserve_model_attempt(
            cfg,
            "run-1",
            role="planner",
            model="model",
            estimated_input_tokens=100,
            output_reserve_tokens=256,
        )

    status = run_budget_status(cfg, "run-1")
    assert status.model_attempts_reserved == 1
    assert status.estimated_tokens_reserved == 356


def test_next_request_is_blocked_before_estimated_token_overrun(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tokens=1_024)
    initialize_run_budget(cfg, "run-1", started_at=datetime.now(UTC))

    with pytest.raises(RunBudgetExceeded):
        reserve_model_attempt(
            cfg,
            "run-1",
            role="planner",
            model="model",
            estimated_input_tokens=800,
            output_reserve_tokens=512,
        )

    status = run_budget_status(cfg, "run-1")
    assert status.model_attempts_reserved == 0
    assert status.estimated_tokens_reserved == 0


def test_wall_time_blocks_resume_but_model_limit_does_not_block_deterministic_finish(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path, attempts=1)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    initialize_run_budget(cfg, "run-1", started_at=started)
    reserve_model_attempt(
        cfg,
        "run-1",
        role="planner",
        model="model",
        estimated_input_tokens=1,
        output_reserve_tokens=256,
        now=started + timedelta(seconds=1),
    )

    assert_run_wall_time(cfg, "run-1", now=started + timedelta(seconds=599))
    with pytest.raises(RunBudgetExceeded):
        assert_run_wall_time(cfg, "run-1", now=started + timedelta(seconds=600))


def test_corrupt_budget_ledger_fails_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    initialize_run_budget(cfg, "run-1", started_at=datetime.now(UTC))
    budget_path(cfg, "run-1").write_text("not-json", encoding="utf-8")

    with pytest.raises(RunBudgetIntegrityError):
        run_budget_status(cfg, "run-1")


def test_provider_retry_cannot_bypass_model_attempt_budget(tmp_path: Path) -> None:
    cfg = _config(tmp_path, attempts=1)
    run_id = "run-provider-budget"
    initialize_run_budget(cfg, run_id, started_at=datetime.now(UTC))
    adapter = OpenCodeAdapter(cfg)
    failed = types.SimpleNamespace(returncode=91, stdout="provider died")
    token = bind_run_id(run_id)
    try:
        with patch(
            "converge_orchestrator.opencode.ExecutionSandbox.run",
            return_value=failed,
        ) as runner:
            with pytest.raises(RunBudgetExceeded):
                adapter.invoke("planner", "Plan one bounded task", cfg.repo_path)
    finally:
        reset_run_id(token)

    assert runner.call_count == 1
    status = run_budget_status(cfg, run_id)
    assert status.model_attempts_reserved == 1
    assert status.estimated_tokens_reserved > 0
    persisted = json.loads(budget_path(cfg, run_id).read_text(encoding="utf-8"))
    assert persisted["model_attempts_reserved"] == 1
