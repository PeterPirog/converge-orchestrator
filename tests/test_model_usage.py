from __future__ import annotations

import json
import types
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.budget import (
    bind_run_id,
    initialize_run_budget,
    reserve_model_attempt,
    reset_run_id,
)
from converge_orchestrator.model_usage import (
    parse_opencode_output,
    record_model_usage_attempt,
    summarize_model_usage,
)
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("REQ-001 Remain measurable.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        run_budget={
            "max_wall_time_seconds": 600,
            "max_model_attempts": 10,
            "max_estimated_tokens": 100_000,
        },
        model_gateway={
            "kind": "openwebui",
            "base_url": "http://127.0.0.1:3000/api",
        },
        model_profiles={
            "primary": {
                "model": "primary-model",
                "context_tokens": 32_000,
            }
        },
        agents={
            "planner": {
                "agent": "converge-planner",
                "model_profile": "primary",
            }
        },
    )


def _event(event_type: str, part: dict) -> str:
    return json.dumps(
        {
            "type": event_type,
            "timestamp": 1,
            "sessionID": "ses_1",
            "part": part,
        }
    )


def _step(*, cost: str, input_tokens: int, output_tokens: int) -> str:
    return _event(
        "step_finish",
        {
            "id": "part_1",
            "sessionID": "ses_1",
            "messageID": "msg_1",
            "type": "step-finish",
            "reason": "stop",
            "cost": cost,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": 3,
                "cache": {"read": 5, "write": 7},
            },
        },
    )


def test_json_events_restore_agent_text_and_sum_provider_usage() -> None:
    stdout = "\n".join(
        [
            _event("text", {"type": "text", "text": '{"task": "bounded"}'}),
            _step(cost="0.0012", input_tokens=100, output_tokens=20),
            _step(cost="0.0023", input_tokens=40, output_tokens=10),
        ]
    )

    parsed = parse_opencode_output(stdout)

    assert parsed.text == '{"task": "bounded"}'
    assert parsed.format == "json"
    assert parsed.usage_status == "reported"
    assert parsed.usage is not None
    assert parsed.usage.steps == 2
    assert parsed.usage.tokens.model_dump() == {
        "input": 140,
        "output": 30,
        "reasoning": 6,
        "cache_read": 10,
        "cache_write": 14,
    }
    assert parsed.usage.cost_usd == Decimal("0.0035")


def test_plain_and_malformed_event_output_never_claim_measured_usage() -> None:
    plain = parse_opencode_output("legacy executor output")
    malformed = parse_opencode_output(
        _event("text", {"type": "text", "text": "usable"})
        + "\nnot-json\n"
        + _event("step_finish", {"type": "step-finish", "tokens": {}})
    )

    assert plain.format == "plain"
    assert plain.text == "legacy executor output"
    assert plain.usage_status == "unavailable"
    assert malformed.format == "json"
    assert malformed.text == "usable"
    assert malformed.usage_status == "invalid"
    assert malformed.usage is None


def test_attempt_records_are_idempotent_and_aggregate_by_role_and_model(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    initialize_run_budget(cfg, "run-1", started_at=datetime.now(UTC))
    for _ in range(2):
        reserve_model_attempt(
            cfg,
            "run-1",
            role="planner",
            model="openwebui/primary-model",
            estimated_input_tokens=1,
            output_reserve_tokens=1,
        )
    reported = parse_opencode_output(_step(cost="0.01", input_tokens=12, output_tokens=4))
    unavailable = parse_opencode_output("legacy")

    first = record_model_usage_attempt(
        cfg,
        "run-1",
        reservation_attempt=1,
        role="planner",
        model="openwebui/primary-model",
        model_profile="primary",
        returncode=0,
        parsed=reported,
    )
    repeated = record_model_usage_attempt(
        cfg,
        "run-1",
        reservation_attempt=1,
        role="planner",
        model="openwebui/primary-model",
        model_profile="primary",
        returncode=0,
        parsed=reported,
    )
    record_model_usage_attempt(
        cfg,
        "run-1",
        reservation_attempt=2,
        role="planner",
        model="openwebui/fallback-model",
        model_profile="fallback",
        returncode=1,
        parsed=unavailable,
    )

    summary = summarize_model_usage(cfg, "run-1")

    assert repeated.recorded_at == first.recorded_at
    assert summary.attempts == 2
    assert summary.reservations == 2
    assert summary.unrecorded_attempts == 0
    assert summary.reported_attempts == 1
    assert summary.coverage_complete is False
    assert summary.totals.attempts == 2
    assert summary.totals.tokens.input == 12
    assert summary.totals.cost_usd == Decimal("0.01")
    assert summary.by_role["planner"].attempts == 2
    assert summary.by_model["openwebui/primary-model"].reported_attempts == 1
    assert summary.by_model["openwebui/fallback-model"].reported_attempts == 0


def test_opencode_invocation_requests_json_and_persists_reservation_bound_usage(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    run_id = "run-opencode-usage"
    initialize_run_budget(cfg, run_id, started_at=datetime.now(UTC))
    stdout = "\n".join(
        [
            _event("text", {"type": "text", "text": '{"task": "ok"}'}),
            _step(cost="0.0042", input_tokens=321, output_tokens=45),
        ]
    )
    response = types.SimpleNamespace(returncode=0, stdout=stdout)
    token = bind_run_id(run_id)
    try:
        with patch(
            "converge_orchestrator.opencode.ExecutionSandbox.run",
            return_value=response,
        ) as runner:
            result = OpenCodeAdapter(cfg).invoke(
                "planner", "Plan one bounded task", cfg.repo_path
            )
    finally:
        reset_run_id(token)

    command = runner.call_args.args[0]
    assert command[command.index("--format") + 1] == "json"
    assert result.output == '{"task": "ok"}'
    assert result.context["provider_usage_status"] == "reported"
    assert result.context["provider_usage"]["cost_usd"] == "0.0042"
    assert result.context["provider_usage_persistence"] == "recorded"
    assert result.context["provider_usage_record"] == "model-usage/attempt-000001.json"

    summary = summarize_model_usage(cfg, run_id)
    assert summary.coverage_complete is True
    assert summary.totals.tokens.input == 321
    assert summary.totals.cost_usd == Decimal("0.0042")


def test_usage_write_failure_does_not_retry_a_successful_provider_call(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    run_id = "run-usage-write-failure"
    initialize_run_budget(cfg, run_id, started_at=datetime.now(UTC))
    response = types.SimpleNamespace(
        returncode=0,
        stdout="\n".join(
            [
                _event("text", {"type": "text", "text": "ok"}),
                _step(cost="0.001", input_tokens=1, output_tokens=1),
            ]
        ),
    )
    token = bind_run_id(run_id)
    try:
        with (
            patch(
                "converge_orchestrator.opencode.ExecutionSandbox.run",
                return_value=response,
            ) as runner,
            patch(
                "converge_orchestrator.opencode.record_model_usage_attempt",
                side_effect=OSError("disk unavailable"),
            ),
        ):
            result = OpenCodeAdapter(cfg).invoke("planner", "Plan", cfg.repo_path)
    finally:
        reset_run_id(token)

    assert result.ok is True
    assert result.output == "ok"
    assert runner.call_count == 1
    assert result.context["provider_usage_persistence"] == "failed"
