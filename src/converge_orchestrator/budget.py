from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .models import ProjectConfig

_BUDGET_LOCK = threading.Lock()
_CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("converge_run_id", default=None)


class RunBudgetLedger(BaseModel):
    """Crash-safe counters for the bounded resource envelope of one durable run."""

    version: int = 1
    run_id: str
    started_at: datetime
    model_attempts_reserved: int = Field(default=0, ge=0)
    estimated_tokens_reserved: int = Field(default=0, ge=0)
    updated_at: datetime


class RunBudgetStatus(BaseModel):
    run_id: str
    elapsed_seconds: float
    max_wall_time_seconds: int
    model_attempts_reserved: int
    max_model_attempts: int
    estimated_tokens_reserved: int
    max_estimated_tokens: int
    exhausted: bool
    reason: str | None = None


class RunBudgetExceeded(RuntimeError):
    """Raised before additional autonomous work would exceed the pinned run envelope."""

    def __init__(self, status: RunBudgetStatus):
        self.status = status
        reason = status.reason or "resource budget exhausted"
        super().__init__(f"RUN_RESOURCE_BUDGET_EXHAUSTED: {reason}")


class RunBudgetIntegrityError(RuntimeError):
    """Budget evidence is missing or malformed after a run has started."""


def bind_run_id(run_id: str) -> Token[str | None]:
    return _CURRENT_RUN_ID.set(run_id)


def reset_run_id(token: Token[str | None]) -> None:
    _CURRENT_RUN_ID.reset(token)


def current_run_id() -> str | None:
    return _CURRENT_RUN_ID.get()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def budget_path(config: ProjectConfig, run_id: str) -> Path:
    return config.state_dir / "evidence" / run_id / "run-budget.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    temporary.replace(path)


def _read(path: Path, run_id: str) -> RunBudgetLedger:
    if not path.is_file():
        raise RunBudgetIntegrityError(f"run budget ledger missing for {run_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ledger = RunBudgetLedger.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise RunBudgetIntegrityError(f"run budget ledger invalid for {run_id}") from exc
    if ledger.version != 1 or ledger.run_id != run_id:
        raise RunBudgetIntegrityError(f"run budget ledger identity mismatch for {run_id}")
    return ledger


def initialize_run_budget(
    config: ProjectConfig,
    run_id: str,
    *,
    started_at: datetime | str,
) -> RunBudgetLedger:
    """Create one immutable-start budget ledger; never reset existing consumption."""

    path = budget_path(config, run_id)
    with _BUDGET_LOCK:
        if path.exists():
            return _read(path, run_id)
        now = _utcnow()
        ledger = RunBudgetLedger(
            run_id=run_id,
            started_at=_coerce_utc(started_at),
            updated_at=now,
        )
        _atomic_write(path, ledger.model_dump(mode="json"))
        return ledger


def _status(
    config: ProjectConfig,
    ledger: RunBudgetLedger,
    *,
    now: datetime | None = None,
    reason: str | None = None,
    include_model_limits: bool = True,
) -> RunBudgetStatus:
    current = _coerce_utc(now or _utcnow())
    elapsed = max(0.0, (current - ledger.started_at).total_seconds())
    budget = config.run_budget
    exhausted_reason = reason
    if exhausted_reason is None and elapsed >= budget.max_wall_time_seconds:
        exhausted_reason = (
            f"wall time {elapsed:.1f}s reached limit {budget.max_wall_time_seconds}s"
        )
    elif (
        include_model_limits
        and exhausted_reason is None
        and ledger.model_attempts_reserved >= budget.max_model_attempts
    ):
        exhausted_reason = (
            "model attempt reservations reached limit "
            f"{budget.max_model_attempts}"
        )
    elif (
        include_model_limits
        and exhausted_reason is None
        and ledger.estimated_tokens_reserved >= budget.max_estimated_tokens
    ):
        exhausted_reason = (
            "estimated model token reservations reached limit "
            f"{budget.max_estimated_tokens}"
        )
    return RunBudgetStatus(
        run_id=ledger.run_id,
        elapsed_seconds=elapsed,
        max_wall_time_seconds=budget.max_wall_time_seconds,
        model_attempts_reserved=ledger.model_attempts_reserved,
        max_model_attempts=budget.max_model_attempts,
        estimated_tokens_reserved=ledger.estimated_tokens_reserved,
        max_estimated_tokens=budget.max_estimated_tokens,
        exhausted=exhausted_reason is not None,
        reason=exhausted_reason,
    )


def run_budget_status(
    config: ProjectConfig,
    run_id: str,
    *,
    now: datetime | None = None,
) -> RunBudgetStatus:
    with _BUDGET_LOCK:
        return _status(config, _read(budget_path(config, run_id), run_id), now=now)


def assert_run_budget(
    config: ProjectConfig,
    run_id: str,
    *,
    now: datetime | None = None,
) -> RunBudgetStatus:
    status = run_budget_status(config, run_id, now=now)
    if status.exhausted:
        raise RunBudgetExceeded(status)
    return status


def assert_run_wall_time(
    config: ProjectConfig,
    run_id: str,
    *,
    now: datetime | None = None,
) -> RunBudgetStatus:
    """Gate controller resume while allowing deterministic completion at model limits."""

    with _BUDGET_LOCK:
        status = _status(
            config,
            _read(budget_path(config, run_id), run_id),
            now=now,
            include_model_limits=False,
        )
    if status.exhausted:
        raise RunBudgetExceeded(status)
    return status


def reserve_model_attempt(
    config: ProjectConfig,
    run_id: str,
    *,
    role: str,
    model: str | None,
    estimated_input_tokens: int,
    output_reserve_tokens: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve an attempt and its deterministic request envelope before provider execution.

    Reservations are deliberately not rolled back after process/provider failure. Retries and crash
    recovery therefore consume the same finite envelope instead of accidentally granting fresh budget.
    """

    if estimated_input_tokens < 0 or output_reserve_tokens < 0:
        raise ValueError("model token reservation values must be non-negative")
    requested_tokens = estimated_input_tokens + output_reserve_tokens
    path = budget_path(config, run_id)
    with _BUDGET_LOCK:
        ledger = _read(path, run_id)
        current = _coerce_utc(now or _utcnow())
        status = _status(config, ledger, now=current)
        if status.exhausted:
            raise RunBudgetExceeded(status)

        budget = config.run_budget
        next_attempts = ledger.model_attempts_reserved + 1
        if next_attempts > budget.max_model_attempts:
            raise RunBudgetExceeded(
                _status(
                    config,
                    ledger,
                    now=current,
                    reason=(
                        "next model attempt would exceed limit "
                        f"{budget.max_model_attempts}"
                    ),
                )
            )
        next_tokens = ledger.estimated_tokens_reserved + requested_tokens
        if next_tokens > budget.max_estimated_tokens:
            raise RunBudgetExceeded(
                _status(
                    config,
                    ledger,
                    now=current,
                    reason=(
                        f"next model request reserves {requested_tokens} estimated tokens; "
                        f"{ledger.estimated_tokens_reserved} already reserved against limit "
                        f"{budget.max_estimated_tokens}"
                    ),
                )
            )

        updated = ledger.model_copy(
            update={
                "model_attempts_reserved": next_attempts,
                "estimated_tokens_reserved": next_tokens,
                "updated_at": current,
            }
        )
        _atomic_write(path, updated.model_dump(mode="json"))
        return {
            "run_id": run_id,
            "role": role,
            "model": model,
            "attempt": next_attempts,
            "estimated_input_tokens": estimated_input_tokens,
            "output_reserve_tokens": output_reserve_tokens,
            "estimated_tokens_reserved_for_attempt": requested_tokens,
            "estimated_tokens_reserved_total": next_tokens,
            "max_model_attempts": budget.max_model_attempts,
            "max_estimated_tokens": budget.max_estimated_tokens,
        }
