from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .budget import RunBudgetLedger, budget_path
from .models import ProjectConfig

_USAGE_LOCK = threading.Lock()


class TokenUsage(BaseModel):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    reasoning: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)


class ProviderUsage(BaseModel):
    """Provider-reported usage from all completed OpenCode steps in one invocation."""

    steps: int = Field(ge=1)
    tokens: TokenUsage
    cost_usd: Decimal = Field(ge=0)


class ParsedOpenCodeOutput(BaseModel):
    text: str
    format: Literal["json", "plain"]
    usage_status: Literal["reported", "unavailable", "invalid"]
    usage: ProviderUsage | None = None

    @model_validator(mode="after")
    def usage_matches_status(self) -> ParsedOpenCodeOutput:
        if (self.usage_status == "reported") != (self.usage is not None):
            raise ValueError("reported usage status and usage payload must agree")
        return self


class ModelUsageAttempt(BaseModel):
    version: Literal[1] = 1
    run_id: str
    reservation_attempt: int = Field(ge=1)
    role: str
    model: str | None = None
    model_profile: str | None = None
    returncode: int
    usage_status: Literal["reported", "unavailable", "invalid"]
    usage: ProviderUsage | None = None
    recorded_at: datetime

    @model_validator(mode="after")
    def usage_matches_status(self) -> ModelUsageAttempt:
        if (self.usage_status == "reported") != (self.usage is not None):
            raise ValueError("reported usage status and usage payload must agree")
        return self


class UsageTotals(BaseModel):
    attempts: int = Field(default=0, ge=0)
    reported_attempts: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    tokens: TokenUsage = Field(
        default_factory=lambda: TokenUsage(input=0, output=0)
    )
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)


class RunModelUsageSummary(BaseModel):
    version: Literal[1] = 1
    run_id: str
    reservations: int | None = Field(default=None, ge=0)
    attempts: int = Field(ge=0)
    reported_attempts: int = Field(ge=0)
    unrecorded_attempts: int | None = Field(default=None, ge=0)
    coverage_complete: bool
    totals: UsageTotals
    by_role: dict[str, UsageTotals]
    by_model: dict[str, UsageTotals]


class ModelUsageIntegrityError(RuntimeError):
    """Durable provider-usage evidence is missing, malformed or contradictory."""


class _CacheTokens(BaseModel):
    read: int = Field(default=0, ge=0)
    write: int = Field(default=0, ge=0)


class _OpenCodeTokens(BaseModel):
    input: int = Field(ge=0)
    output: int = Field(ge=0)
    reasoning: int = Field(default=0, ge=0)
    cache: _CacheTokens = Field(default_factory=_CacheTokens)


class _StepFinish(BaseModel):
    type: Literal["step-finish"]
    cost: Decimal = Field(ge=0)
    tokens: _OpenCodeTokens


def parse_opencode_output(stdout: str) -> ParsedOpenCodeOutput:
    """Extract agent text and measured usage from stable OpenCode JSON events.

    Plain output remains supported for older/test executors. Once any OpenCode event is
    recognized, malformed JSON or a malformed step-finish makes telemetry invalid rather than
    silently publishing partial cost data. Agent text remains usable because telemetry is
    observational and cannot weaken or block the conservative run budget.
    """

    recognized = 0
    invalid = False
    text_parts: list[str] = []
    error_parts: list[str] = []
    step_usages: list[_StepFinish] = []

    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            invalid = True
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            invalid = True
            continue
        recognized += 1
        event_type = event["type"]
        if event_type == "text":
            part = event.get("part")
            if not isinstance(part, dict) or part.get("type") != "text":
                invalid = True
                continue
            value = part.get("text")
            if not isinstance(value, str):
                invalid = True
                continue
            if value.strip():
                text_parts.append(value.strip())
        elif event_type == "step_finish":
            try:
                step_usages.append(_StepFinish.model_validate(event.get("part")))
            except ValidationError:
                invalid = True
        elif event_type == "error":
            error = event.get("error")
            error_parts.append(
                error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            )

    if recognized == 0:
        return ParsedOpenCodeOutput(
            text=stdout,
            format="plain",
            usage_status="unavailable",
        )

    text = "\n".join(text_parts)
    if not text and error_parts:
        text = "\n".join(error_parts)
    if invalid:
        return ParsedOpenCodeOutput(text=text, format="json", usage_status="invalid")
    if not step_usages:
        return ParsedOpenCodeOutput(text=text, format="json", usage_status="unavailable")

    tokens = TokenUsage(
        input=sum(step.tokens.input for step in step_usages),
        output=sum(step.tokens.output for step in step_usages),
        reasoning=sum(step.tokens.reasoning for step in step_usages),
        cache_read=sum(step.tokens.cache.read for step in step_usages),
        cache_write=sum(step.tokens.cache.write for step in step_usages),
    )
    usage = ProviderUsage(
        steps=len(step_usages),
        tokens=tokens,
        cost_usd=sum((step.cost for step in step_usages), Decimal("0")),
    )
    return ParsedOpenCodeOutput(
        text=text,
        format="json",
        usage_status="reported",
        usage=usage,
    )


def model_usage_dir(config: ProjectConfig, run_id: str) -> Path:
    return config.state_dir / "evidence" / run_id / "model-usage"


def _attempt_path(config: ProjectConfig, run_id: str, reservation_attempt: int) -> Path:
    return model_usage_dir(config, run_id) / f"attempt-{reservation_attempt:06d}.json"


def _reserved_attempts(config: ProjectConfig, run_id: str) -> int | None:
    path = budget_path(config, run_id)
    if not path.exists():
        return None
    try:
        ledger = RunBudgetLedger.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ModelUsageIntegrityError(f"run budget ledger invalid for {run_id}") from exc
    if ledger.version != 1 or ledger.run_id != run_id:
        raise ModelUsageIntegrityError(f"run budget ledger identity mismatch for {run_id}")
    return ledger.model_attempts_reserved


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def record_model_usage_attempt(
    config: ProjectConfig,
    run_id: str,
    *,
    reservation_attempt: int,
    role: str,
    model: str | None,
    model_profile: str | None,
    returncode: int,
    parsed: ParsedOpenCodeOutput,
) -> ModelUsageAttempt:
    """Persist one reservation-bound record before orchestration consumes the agent result."""

    record = ModelUsageAttempt(
        run_id=run_id,
        reservation_attempt=reservation_attempt,
        role=role,
        model=model,
        model_profile=model_profile,
        returncode=returncode,
        usage_status=parsed.usage_status,
        usage=parsed.usage,
        recorded_at=datetime.now(UTC),
    )
    path = _attempt_path(config, run_id, reservation_attempt)
    with _USAGE_LOCK:
        reservations = _reserved_attempts(config, run_id)
        if reservations is None or reservation_attempt > reservations:
            raise ModelUsageIntegrityError(
                f"model usage attempt {reservation_attempt} has no durable budget reservation"
            )
        if path.exists():
            try:
                existing = ModelUsageAttempt.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError) as exc:
                raise ModelUsageIntegrityError(
                    f"model usage attempt {reservation_attempt} is invalid"
                ) from exc
            comparable = {"recorded_at"}
            if existing.model_dump(exclude=comparable) != record.model_dump(exclude=comparable):
                raise ModelUsageIntegrityError(
                    f"model usage attempt {reservation_attempt} conflicts with durable evidence"
                )
            return existing
        _atomic_write(path, record.model_dump(mode="json"))
    return record


def _add_usage(total: UsageTotals, record: ModelUsageAttempt) -> UsageTotals:
    usage = record.usage
    tokens = total.tokens
    return total.model_copy(
        update={
            "attempts": total.attempts + 1,
            "reported_attempts": total.reported_attempts + (1 if usage else 0),
            "steps": total.steps + (usage.steps if usage else 0),
            "tokens": tokens.model_copy(
                update={
                    "input": tokens.input + (usage.tokens.input if usage else 0),
                    "output": tokens.output + (usage.tokens.output if usage else 0),
                    "reasoning": tokens.reasoning + (usage.tokens.reasoning if usage else 0),
                    "cache_read": tokens.cache_read + (usage.tokens.cache_read if usage else 0),
                    "cache_write": tokens.cache_write + (usage.tokens.cache_write if usage else 0),
                }
            ),
            "cost_usd": total.cost_usd + (usage.cost_usd if usage else Decimal("0")),
        }
    )


def summarize_model_usage(config: ProjectConfig, run_id: str) -> RunModelUsageSummary:
    root = model_usage_dir(config, run_id)
    records: list[ModelUsageAttempt] = []
    with _USAGE_LOCK:
        reservations = _reserved_attempts(config, run_id)
        if root.exists():
            for path in sorted(root.glob("attempt-*.json")):
                try:
                    record = ModelUsageAttempt.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, ValidationError) as exc:
                    raise ModelUsageIntegrityError(
                        f"model usage evidence is invalid: {path.name}"
                    ) from exc
                if record.run_id != run_id:
                    raise ModelUsageIntegrityError(
                        f"model usage evidence identity mismatch: {path.name}"
                    )
                if path != _attempt_path(config, run_id, record.reservation_attempt):
                    raise ModelUsageIntegrityError(
                        f"model usage reservation identity mismatch: {path.name}"
                    )
                records.append(record)

        if records and reservations is None:
            raise ModelUsageIntegrityError(
                f"model usage evidence exists without a run budget ledger for {run_id}"
            )
        if reservations is not None and len(records) > reservations:
            raise ModelUsageIntegrityError(
                f"model usage evidence exceeds durable reservations for {run_id}"
            )

    totals = UsageTotals()
    by_role: defaultdict[str, UsageTotals] = defaultdict(UsageTotals)
    by_model: defaultdict[str, UsageTotals] = defaultdict(UsageTotals)
    for record in records:
        totals = _add_usage(totals, record)
        by_role[record.role] = _add_usage(by_role[record.role], record)
        model_key = record.model or "unresolved"
        by_model[model_key] = _add_usage(by_model[model_key], record)

    return RunModelUsageSummary(
        run_id=run_id,
        reservations=reservations,
        attempts=len(records),
        reported_attempts=totals.reported_attempts,
        unrecorded_attempts=(reservations - len(records) if reservations is not None else None),
        coverage_complete=(
            reservations is not None
            and bool(records)
            and len(records) == reservations
            and totals.reported_attempts == len(records)
        ),
        totals=totals,
        by_role=dict(sorted(by_role.items())),
        by_model=dict(sorted(by_model.items())),
    )
