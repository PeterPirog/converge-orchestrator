from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from converge_orchestrator.context import prepare_prompt
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter


class _Result:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def _config(tmp_path: Path, *, role: str = "planner") -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        model_gateway={
            "kind": "openwebui",
            "base_url": "http://127.0.0.1:3000/api",
        },
        model_profiles={
            "primary": {
                "model": "primary-model",
                "context_tokens": 8000,
                "output_tokens": 1000,
                "request_body": {"temperature": 0.1},
            },
            "fallback": {
                "model": "fallback-model",
                "context_tokens": 4000,
                "output_tokens": 1500,
                "request_body": {"temperature": 0.2},
            },
        },
        agents={
            role: {
                "agent": f"converge-{role}",
                "model_profile": "primary",
                "fallback_model_profiles": ["fallback"],
            }
        },
        context_input_fraction=0.5,
        context_output_reserve_tokens=1000,
    )


def _model_from_command(command: list[str]) -> str:
    return command[command.index("--model") + 1]


def test_fallback_chain_uses_smallest_context_budget(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    _, report = prepare_prompt(cfg, "planner", "authoritative core")

    assert report.context_limit_tokens == 4000
    assert report.output_reserve_tokens == 1500
    assert report.input_budget_tokens == 2000


def test_builder_model_fallback_is_rejected_without_deterministic_rollback(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="builder fallback_model_profiles are forbidden"):
        _config(tmp_path, role="builder")


def test_fallback_chain_requires_known_profiles_and_context_limits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("Requirement.\n", encoding="utf-8")
    common = {
        "repo_path": repo,
        "requirements_path": requirements,
        "require_spec_read_only": False,
    }

    with pytest.raises(ValidationError, match="unknown model profiles"):
        ProjectConfig(
            **common,
            model_profiles={
                "primary": {"model": "primary", "context_tokens": 8000},
            },
            agents={
                "planner": {
                    "agent": "converge-planner",
                    "model_profile": "primary",
                    "fallback_model_profiles": ["missing"],
                }
            },
        )

    with pytest.raises(ValidationError, match="requires context_tokens"):
        ProjectConfig(
            **common,
            model_profiles={
                "primary": {"model": "primary", "context_tokens": 8000},
                "fallback": {"model": "fallback"},
            },
            agents={
                "planner": {
                    "agent": "converge-planner",
                    "model_profile": "primary",
                    "fallback_model_profiles": ["fallback"],
                }
            },
        )


def test_primary_success_does_not_pay_fallback_cost(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, return_value=_Result(0, "ok")) as runner:
        result = adapter.invoke("planner", "same target", cfg.repo_path)

    assert result.ok
    runner.assert_called_once()
    assert _model_from_command(runner.call_args.args[0]) == "openwebui/primary-model"
    assert result.context["model_attempts"] == [
        {
            "attempt": 1,
            "profile": "primary",
            "model": "openwebui/primary-model",
            "variant": None,
            "fallback": False,
            "outcome": "success",
            "returncode": 0,
            "error_type": None,
        }
    ]


def test_nonzero_primary_retries_same_prompt_on_explicit_fallback(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(
        target,
        side_effect=[_Result(70, "provider unavailable"), _Result(0, "fallback ok")],
    ) as runner:
        result = adapter.invoke("planner", "immutable-target-prompt", cfg.repo_path)

    assert result.ok
    assert result.output == "fallback ok"
    assert runner.call_count == 2
    first, second = runner.call_args_list
    first_command = first.args[0]
    second_command = second.args[0]
    assert _model_from_command(first_command) == "openwebui/primary-model"
    assert _model_from_command(second_command) == "openwebui/fallback-model"
    assert first_command[-1] == second_command[-1] == "immutable-target-prompt"
    assert first.kwargs["writable_cwd"] is False
    assert second.kwargs["writable_cwd"] is False

    second_runtime = json.loads(second.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    fallback_agent = second_runtime["agent"]["converge-planner"]
    assert fallback_agent["model"] == "openwebui/fallback-model"
    assert fallback_agent["temperature"] == 0.2

    attempts = result.context["model_attempts"]
    assert [item["outcome"] for item in attempts] == ["nonzero", "success"]
    assert attempts[1]["fallback"] is True

    ledger = cfg.state_dir / "context-usage.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["model_attempts"] == attempts


def test_exception_primary_can_recover_with_read_only_fallback(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(
        target,
        side_effect=[TimeoutError("gateway timeout"), _Result(0, "fallback ok")],
    ):
        result = adapter.invoke("planner", "same prompt", cfg.repo_path)

    assert result.ok
    assert result.context["model_attempts"][0]["outcome"] == "exception"
    assert result.context["model_attempts"][0]["error_type"] == "TimeoutError"
    assert result.context["model_attempts"][1]["outcome"] == "success"


def test_exhausted_nonzero_fallback_chain_preserves_failure(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(
        target,
        side_effect=[_Result(70, "primary failed"), _Result(71, "fallback failed")],
    ):
        result = adapter.invoke("planner", "same prompt", cfg.repo_path)

    assert not result.ok
    assert result.returncode == 71
    assert result.output == "fallback failed"
    assert [item["returncode"] for item in result.context["model_attempts"]] == [70, 71]


def test_exhausted_exception_chain_preserves_existing_exception_semantics(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(
        target,
        side_effect=[TimeoutError("primary"), RuntimeError("fallback")],
    ):
        with pytest.raises(RuntimeError, match="fallback"):
            adapter.invoke("planner", "same prompt", cfg.repo_path)

    ledger = cfg.state_dir / "context-usage.jsonl"
    record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert [item["error_type"] for item in record["model_attempts"]] == [
        "TimeoutError",
        "RuntimeError",
    ]
