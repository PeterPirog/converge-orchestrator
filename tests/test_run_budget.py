from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from converge_orchestrator.budget import (
    RunBudgetExceeded,
    check_run_wall_time,
    ensure_run_budget,
    read_run_budget,
    reserve_model_attempt,
)
from converge_orchestrator.config import (
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.models import ProjectConfig


def _raw_config(tmp_path: Path, *, attempts: int = 3, seconds: int = 300) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain deterministic.\n", encoding="utf-8")
    return {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "state_dir": str(tmp_path / "state"),
            "worktree_dir": str(tmp_path / "state" / "worktrees"),
            "require_spec_read_only": False,
        },
        "agents": {"planner": {"agent": "planner"}},
        "workflow": {
            "max_run_seconds": seconds,
            "max_model_attempts": attempts,
        },
    }


def _runtime_config(
    tmp_path: Path,
    *,
    attempts: int = 3,
    seconds: int = 300,
) -> ProjectConfig:
    source = tmp_path / "converge.yaml"
    source.write_text(
        yaml.safe_dump(_raw_config(tmp_path, attempts=attempts, seconds=seconds)),
        encoding="utf-8",
    )
    _, snapshot, digest = materialize_run_config_snapshot(source, "run-budget-test")
    return load_run_config_snapshot(snapshot, digest)


def test_documented_budget_fields_are_validated_and_pinned(tmp_path: Path) -> None:
    cfg = _runtime_config(tmp_path, attempts=7, seconds=900)

    assert cfg.max_model_attempts == 7
    assert cfg.max_run_seconds == 900
    assert cfg._runtime_run_id == "run-budget-test"

    raw = _raw_config(tmp_path / "invalid", attempts=0, seconds=59)
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(raw)


def test_budget_initialization_is_idempotent_across_restart(tmp_path: Path) -> None:
    cfg = _runtime_config(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=UTC)

    first = ensure_run_budget(cfg, now=started)
    second = ensure_run_budget(cfg, now=started + timedelta(hours=1))

    assert first is not None
    assert second is not None
    assert second.started_at == first.started_at
    assert second.model_attempts_reserved == 0


def test_model_attempt_reservation_is_crash_conservative(tmp_path: Path) -> None:
    cfg = _runtime_config(tmp_path, attempts=2)
    started = datetime(2026, 1, 1, tzinfo=UTC)

    first = reserve_model_attempt(
        cfg,
        role="planner",
        model="provider/planner",
        model_profile=None,
        now=started,
    )
    second = reserve_model_attempt(
        cfg,
        role="builder",
        model="provider/builder",
        model_profile=None,
        now=started + timedelta(seconds=1),
    )

    assert first is not None and first["reservation"] == 1
    assert second is not None and second["reservation"] == 2
    with pytest.raises(RunBudgetExceeded, match="model-attempt limit") as exc_info:
        reserve_model_attempt(
            cfg,
            role="reviewer",
            model="provider/reviewer",
            model_profile=None,
            now=started + timedelta(seconds=2),
        )
    assert exc_info.value.kind == "model_attempts"

    ledger = read_run_budget(cfg)
    assert ledger is not None
    assert ledger["model_attempts_reserved"] == 2
    assert ledger["per_role_attempts"] == {"planner": 1, "builder": 1}
    assert ledger["exhausted_kind"] == "model_attempts"


def test_parallel_review_reservations_cannot_exceed_cap(tmp_path: Path) -> None:
    cfg = _runtime_config(tmp_path, attempts=4)
    started = datetime(2026, 1, 1, tzinfo=UTC)

    def reserve(index: int) -> str:
        try:
            reserve_model_attempt(
                cfg,
                role=f"reviewer-{index}",
                model="provider/reviewer",
                model_profile=None,
                now=started,
            )
        except RunBudgetExceeded:
            return "exhausted"
        return "reserved"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(reserve, range(8)))

    assert outcomes.count("reserved") == 4
    assert outcomes.count("exhausted") == 4
    ledger = read_run_budget(cfg)
    assert ledger is not None
    assert ledger["model_attempts_reserved"] == 4


def test_wall_time_budget_does_not_reset_and_fails_closed(tmp_path: Path) -> None:
    cfg = _runtime_config(tmp_path, seconds=60)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    ensure_run_budget(cfg, now=started)

    status = check_run_wall_time(cfg, now=started + timedelta(seconds=59))
    assert status is not None
    assert status["elapsed_seconds"] == 59

    with pytest.raises(RunBudgetExceeded, match="wall-time limit") as exc_info:
        check_run_wall_time(cfg, now=started + timedelta(seconds=60))
    assert exc_info.value.kind == "run_wall_time"
    ledger = read_run_budget(cfg)
    assert ledger is not None
    assert ledger["exhausted_kind"] == "run_wall_time"
