from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import ProjectConfig

_BUDGET_LOCK = threading.Lock()
_BUDGET_VERSION = 1


class RunBudgetExceeded(RuntimeError):
    """Deterministic terminal policy failure; it must never become an LLM/HITL override."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class RunBudgetLedger(BaseModel):
    version: int = _BUDGET_VERSION
    run_id: str
    started_at: str
    max_run_seconds: int
    max_model_attempts: int
    model_attempts_reserved: int = 0
    per_role_attempts: dict[str, int] = Field(default_factory=dict)
    exhausted_kind: str | None = None
    exhausted_at: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _budget_path(config: ProjectConfig, run_id: str) -> Path:
    return config.state_dir / "run-budgets" / f"{run_id}.json"


def _write_ledger(path: Path, ledger: RunBudgetLedger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_ledger(path: Path) -> RunBudgetLedger:
    try:
        return RunBudgetLedger.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Run budget ledger is unreadable or invalid: {path}") from exc


def _validate_identity(config: ProjectConfig, run_id: str, ledger: RunBudgetLedger) -> None:
    if ledger.version != _BUDGET_VERSION or ledger.run_id != run_id:
        raise RuntimeError("Run budget ledger identity does not match the active run")
    if (
        ledger.max_run_seconds != config.max_run_seconds
        or ledger.max_model_attempts != config.max_model_attempts
    ):
        raise RuntimeError(
            "Pinned run budget policy does not match its durable budget ledger"
        )


def ensure_run_budget(
    config: ProjectConfig,
    *,
    now: datetime | None = None,
) -> RunBudgetLedger | None:
    """Create or validate the durable budget for a pinned run without resetting elapsed time."""
    run_id = config._runtime_run_id
    if not run_id:
        return None
    timestamp = (now or _utcnow()).astimezone(UTC)
    path = _budget_path(config, run_id)
    with _BUDGET_LOCK:
        if path.exists():
            ledger = _load_ledger(path)
            _validate_identity(config, run_id, ledger)
            return ledger
        ledger = RunBudgetLedger(
            run_id=run_id,
            started_at=timestamp.isoformat(),
            max_run_seconds=config.max_run_seconds,
            max_model_attempts=config.max_model_attempts,
        )
        _write_ledger(path, ledger)
        return ledger


def _mark_exhausted(
    path: Path,
    ledger: RunBudgetLedger,
    kind: str,
    timestamp: datetime,
) -> RunBudgetLedger:
    if ledger.exhausted_kind is None:
        ledger = ledger.model_copy(
            update={
                "exhausted_kind": kind,
                "exhausted_at": timestamp.isoformat(),
            }
        )
        _write_ledger(path, ledger)
    return ledger


def _check_elapsed(
    path: Path,
    ledger: RunBudgetLedger,
    timestamp: datetime,
) -> None:
    elapsed = max(0.0, (timestamp - _parse_timestamp(ledger.started_at)).total_seconds())
    if elapsed < ledger.max_run_seconds:
        return
    _mark_exhausted(path, ledger, "run_wall_time", timestamp)
    raise RunBudgetExceeded(
        "run_wall_time",
        "RUN_BUDGET_EXCEEDED: durable run wall-time limit reached "
        f"({int(elapsed)}s >= {ledger.max_run_seconds}s)",
    )


def check_run_wall_time(
    config: ProjectConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Fail closed when a pinned run has exhausted its durable elapsed-time envelope."""
    run_id = config._runtime_run_id
    if not run_id:
        return None
    timestamp = (now or _utcnow()).astimezone(UTC)
    path = _budget_path(config, run_id)
    with _BUDGET_LOCK:
        ledger = ensure_run_budget(config, now=timestamp)
        assert ledger is not None
        _check_elapsed(path, ledger, timestamp)
        elapsed = max(
            0.0,
            (timestamp - _parse_timestamp(ledger.started_at)).total_seconds(),
        )
        return {
            "run_id": run_id,
            "elapsed_seconds": int(elapsed),
            "max_run_seconds": ledger.max_run_seconds,
            "model_attempts_reserved": ledger.model_attempts_reserved,
            "max_model_attempts": ledger.max_model_attempts,
        }


def reserve_model_attempt(
    config: ProjectConfig,
    *,
    role: str,
    model: str | None,
    model_profile: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Durably reserve one actual OpenCode/model attempt before starting the subprocess.

    Reservation is intentionally at-least-once: a process crash after this write but before the
    provider response still consumes the attempt. That conservative rule prevents restart from
    silently resetting model-usage policy.
    """
    run_id = config._runtime_run_id
    if not run_id:
        return None
    timestamp = (now or _utcnow()).astimezone(UTC)
    path = _budget_path(config, run_id)
    with _BUDGET_LOCK:
        if path.exists():
            ledger = _load_ledger(path)
            _validate_identity(config, run_id, ledger)
        else:
            ledger = RunBudgetLedger(
                run_id=run_id,
                started_at=timestamp.isoformat(),
                max_run_seconds=config.max_run_seconds,
                max_model_attempts=config.max_model_attempts,
            )
            _write_ledger(path, ledger)

        _check_elapsed(path, ledger, timestamp)
        if ledger.model_attempts_reserved >= ledger.max_model_attempts:
            _mark_exhausted(path, ledger, "model_attempts", timestamp)
            raise RunBudgetExceeded(
                "model_attempts",
                "RUN_BUDGET_EXCEEDED: durable model-attempt limit reached "
                f"({ledger.model_attempts_reserved} >= {ledger.max_model_attempts})",
            )

        per_role = dict(ledger.per_role_attempts)
        per_role[role] = per_role.get(role, 0) + 1
        reservation = ledger.model_attempts_reserved + 1
        ledger = ledger.model_copy(
            update={
                "model_attempts_reserved": reservation,
                "per_role_attempts": per_role,
            }
        )
        _write_ledger(path, ledger)
        return {
            "run_id": run_id,
            "reservation": reservation,
            "max_model_attempts": ledger.max_model_attempts,
            "role": role,
            "model": model or "<opencode-default>",
            "model_profile": model_profile,
            "reserved_at": timestamp.isoformat(),
        }


def read_run_budget(config: ProjectConfig) -> dict[str, Any] | None:
    """Return durable budget evidence for diagnostics/tests without mutating the ledger."""
    run_id = config._runtime_run_id
    if not run_id:
        return None
    path = _budget_path(config, run_id)
    if not path.exists():
        return None
    ledger = _load_ledger(path)
    _validate_identity(config, run_id, ledger)
    return ledger.model_dump(mode="json")
