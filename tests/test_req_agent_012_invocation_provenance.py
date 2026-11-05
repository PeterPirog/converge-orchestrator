"""RED-phase pin test for REQ-AGENT-012 model-invocation provenance.

The durable ModelUsageAttempt record must carry gateway identity and SHA-256
identities of the rendered input envelope and parsed model output.  This test
runs fully offline by mocking the sandbox execution path.
"""

from __future__ import annotations

import hashlib
import json
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.budget import (
    bind_run_id,
    initialize_run_budget,
    reset_run_id,
)
from converge_orchestrator.models import (
    AgentConfig,
    ModelGatewayConfig,
    ModelProfile,
    ProjectConfig,
    RunBudgetConfig,
)
from converge_orchestrator.opencode import OpenCodeAdapter


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "requirements.md"
    requirements.write_text("REQ-AGENT-012 Record invocation provenance.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        run_budget=RunBudgetConfig(
            max_wall_time_seconds=600,
            max_model_attempts=10,
            max_estimated_tokens=100_000,
        ),
        model_gateway=ModelGatewayConfig(
            kind="openai_compatible",
            provider_id="test-provider",
            base_url="http://127.0.0.1:9999/v1",
        ),
        model_profiles={
            "primary": ModelProfile(
                model="primary-model",
                context_tokens=32_000,
                output_tokens=4096,
            )
        },
        agents={
            "planner": AgentConfig(
                agent="converge-planner",
                model_profile="primary",
            )
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
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
        },
    )


def test_durable_attempt_records_gateway_identity_and_envelope_digests(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    run_id = "run-req-agent-012-provenance"
    initialize_run_budget(cfg, run_id, started_at=datetime.now(UTC))

    output_text = "REQ-AGENT-012 provenance pin output"
    stdout = "\n".join(
        [
            _event("text", {"type": "text", "text": output_text}),
            _step(cost="0.0001", input_tokens=7, output_tokens=3),
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
                "planner", "Plan one provenance test task", cfg.repo_path
            )
    finally:
        reset_run_id(token)

    assert result.ok is True
    assert result.output == output_text

    rendered_prompt = runner.call_args.kwargs.get("input")
    assert isinstance(rendered_prompt, str), "rendered input envelope must be a string"
    expected_input_sha256 = hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()
    expected_output_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()

    assert cfg.state_dir is not None
    attempt_path = (
        cfg.state_dir / "evidence" / run_id / "model-usage" / "attempt-000001.json"
    )
    assert attempt_path.exists(), "durable model-usage attempt record was not written"
    payload = json.loads(attempt_path.read_text(encoding="utf-8"))

    required_fields = (
        "gateway_kind",
        "gateway_provider_id",
        "input_envelope_sha256",
        "output_sha256",
    )
    missing = [field for field in required_fields if field not in payload]
    if missing:
        raise AssertionError(
            "REQ-AGENT-012 MODEL INVOCATION PROVENANCE MISSING: "
            f"missing fields {missing} in {attempt_path.name}"
        )

    assert payload["gateway_kind"] == cfg.model_gateway.kind
    assert payload["gateway_provider_id"] == cfg.model_gateway.provider_id
    assert payload["input_envelope_sha256"] == expected_input_sha256
    assert payload["output_sha256"] == expected_output_sha256

    blob = attempt_path.read_text(encoding="utf-8")
    assert rendered_prompt not in blob, "raw input envelope must not be stored in durable evidence"
    assert output_text not in blob, "raw model output must not be stored in durable evidence"
