"""Pin REQ-AGENT-009: model profile context capacity check before invocation.

REQ-AGENT-009 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L257
[7. Deterministic architecture and agent roles]): A model profile whose effective
context capacity cannot contain the authoritative context envelope plus output
reserve SHALL be rejected before invocation rather than receiving silently
truncated authoritative content.

These deterministic offline assertions lock the behavior:

* Before any model invocation, the system checks if the model profile's context
  window can accommodate the authoritative core context plus the required output
  reserve. If not, the invocation is rejected with a clear error.
* The check happens BEFORE the model is invoked, not after truncation occurs.
* The error message clearly indicates the context budget issue.
* No silent truncation of authoritative content occurs.
"""

from __future__ import annotations

import pathlib
import types
from unittest.mock import patch

import pytest

from converge_orchestrator.context import ContextBudgetExceeded
from converge_orchestrator.models import (
    AgentConfig,
    ModelGatewayConfig,
    ModelProfile,
    ProjectConfig,
)
from converge_orchestrator.opencode import OpenCodeAdapter


def _make_base_config(tmp_path: pathlib.Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must check context capacity.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path / "repo",
        requirements_path=tmp_path / "architecture.md",
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        context_input_fraction=0.5,
        model_gateway=ModelGatewayConfig(
            kind="openwebui",
            base_url="http://127.0.0.1:3000/api",
            provider_id="openwebui",
        ),
        model_profiles={
            "small_context": ModelProfile(
                model="small-model",
                context_tokens=1000,
                output_tokens=256,
            ),
            "large_context": ModelProfile(
                model="large-model",
                context_tokens=32000,
                output_tokens=4096,
            ),
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="small_context",
            ),
        },
    )


def _make_large_context_config(tmp_path: pathlib.Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must check context capacity.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=tmp_path / "repo",
        requirements_path=tmp_path / "architecture.md",
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        context_input_fraction=0.5,
        model_gateway=ModelGatewayConfig(
            kind="openwebui",
            base_url="http://127.0.0.1:3000/api",
            provider_id="openwebui",
        ),
        model_profiles={
            "large_context": ModelProfile(
                model="large-model",
                context_tokens=32000,
                output_tokens=4096,
            ),
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="large_context",
            ),
        },
    )


def test_req_agent_009_rejects_profile_with_insufficient_context(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-009: A model profile with insufficient context capacity is
    rejected before invocation."""
    cfg = _make_base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    # The small_context profile has only 1000 tokens, which is insufficient for
    # a large core prompt plus output reserve
    result = adapter.invoke("planner", "x" * 5000, cfg.repo_path)

    # The system should reject the request with a context budget error
    assert not result.ok
    assert "CONTEXT_BUDGET_EXCEEDED" in result.output
    assert "context_budget_exceeded" in result.output.lower() or (
        "context_budget_exceeded" in str(result.context).lower()
    )
    assert result.context["budget_status"] in ("invalid_budget", "core_exceeded")


def test_req_agent_009_accepts_profile_with_sufficient_context(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-009: A model profile with sufficient context capacity is
    accepted."""
    # Create a config with a large context profile for the planner
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must check context capacity.\n", encoding="utf-8")
    cfg = ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        context_input_fraction=0.5,
        model_gateway=ModelGatewayConfig(
            kind="openwebui",
            base_url="http://127.0.0.1:3000/api",
            provider_id="openwebui",
        ),
        model_profiles={
            "large_context": ModelProfile(
                model="large-model",
                context_tokens=32000,
                output_tokens=4096,
            ),
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="large_context",
            ),
        },
    )
    adapter = OpenCodeAdapter(cfg)

    # The large_context profile has 32000 tokens, sufficient for a small prompt
    with patch("converge_orchestrator.opencode.ExecutionSandbox.run") as mock_run:
        mock_run.return_value = types.SimpleNamespace(
            returncode=0,
            stdout='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        )
        with patch("converge_orchestrator.opencode.parse_opencode_output") as mock_parse:
            from converge_orchestrator.model_usage import (
                ParsedOpenCodeOutput,
                ProviderUsage,
                TokenUsage,
            )
            mock_parse.return_value = ParsedOpenCodeOutput(
                text='{"verdict": "pass", "findings": [], "confidence": 0.9}',
                format="json",
                usage_status="reported",
                usage=ProviderUsage(
                    steps=1,
                    tokens=TokenUsage(
                        input=100,
                        output=50,
                        reasoning=0,
                        cache_read=0,
                        cache_write=0,
                    ),
                    cost_usd=0,
                ),
                error_event_count=0,
            )
            result = adapter.invoke("planner", "Review this small diff", cfg.repo_path)

    assert result.ok


def test_req_agent_009_rejects_before_invocation_not_after_truncation(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-009: Rejection happens BEFORE model invocation, not after
    truncation."""
    # Create a fresh tmp_path for this test to avoid directory conflicts
    test_tmp = tmp_path / "test_rejects_before"
    test_tmp.mkdir(exist_ok=True)

    # The check should happen in prepare_prompt, which is called before any
    # model invocation
    from converge_orchestrator.context import prepare_prompt

    with pytest.raises(ContextBudgetExceeded) as exc_info:
        prepare_prompt(
            config=_make_base_config(test_tmp),
            role="planner",
            prompt="x" * 5000,
            model_profile="small_context",
        )

    exc = exc_info.value
    # The error should be raised before any model invocation attempt
    assert exc.report.budget_status in ("invalid_budget", "core_exceeded")
    # The error message should mention context budget
    assert "budget" in str(exc).lower() or "context" in str(exc).lower()


def test_req_agent_009_no_silent_truncation_of_authoritative_content(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-009: No silent truncation of authoritative content occurs."""
    cfg = _make_base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    # The system should reject the request, not silently truncate
    result = adapter.invoke("planner", "x" * 5000, cfg.repo_path)

    # The system should reject the request, not silently truncate
    assert not result.ok
    assert "CONTEXT_BUDGET_EXCEEDED" in result.output
    assert "context_budget_exceeded" in result.output.lower() or (
        "context_budget_exceeded" in str(result.context).lower()
    )
    assert result.context["budget_status"] in ("invalid_budget", "core_exceeded")
    # Verify no truncation occurred by checking the report
    assert (
        result.context["estimated_core_tokens"] > result.context["input_budget_tokens"]
        or result.context["budget_status"] == "invalid_budget"
    )