from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter


def _config(
    tmp_path: Path,
    *,
    provider_retries: int = 0,
    primary_context: int = 32000,
    fallback_context: int = 32000,
) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain resilient.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_output_reserve_tokens=256,
        model_gateway={
            "kind": "openwebui",
            "base_url": "http://127.0.0.1:3000/api",
        },
        model_profiles={
            "primary": {
                "model": "primary-model",
                "context_tokens": primary_context,
                "request_body": {"temperature": 0.2},
            },
            "fallback": {
                "model": "fallback-model",
                "context_tokens": fallback_context,
                "request_body": {"temperature": 0.0},
            },
        },
        agents={
            "planner": {
                "agent": "converge-planner",
                "model_profile": "primary",
                "fallback_model_profiles": ["fallback"],
                "provider_retries": provider_retries,
            }
        },
    )


def test_failed_primary_uses_explicit_fallback_with_attempt_evidence(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    responses = [
        types.SimpleNamespace(returncode=1, stdout="provider unavailable"),
        types.SimpleNamespace(returncode=0, stdout='{"task": "ok"}'),
    ]

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, side_effect=responses) as runner:
        result = adapter.invoke("planner", "Plan one task", cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == 2
    commands = [call.args[0] for call in runner.call_args_list]
    assert [command[command.index("--model") + 1] for command in commands] == [
        "openwebui/primary-model",
        "openwebui/fallback-model",
    ]
    assert all("--continue" not in command and "--session" not in command for command in commands)

    runtime_payloads = [
        json.loads(call.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        for call in runner.call_args_list
    ]
    primary = runtime_payloads[0]["agent"]["converge-planner"]
    fallback = runtime_payloads[1]["agent"]["converge-planner"]
    assert primary["model"] == "openwebui/primary-model"
    assert primary["temperature"] == 0.2
    assert fallback["model"] == "openwebui/fallback-model"
    assert fallback["temperature"] == 0.0
    assert fallback["permission"] == primary["permission"]

    assert result.context["fallback_used"] is True
    assert result.context["selected_model"] == "openwebui/fallback-model"
    attempts = result.context["provider_attempts"]
    assert [(item["model_profile"], item["ok"]) for item in attempts] == [
        ("primary", False),
        ("fallback", True),
    ]

    health = cfg.state_dir / "provider-health.jsonl"
    records = [json.loads(line) for line in health.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert [record["model"] for record in records] == [
        "openwebui/primary-model",
        "openwebui/fallback-model",
    ]
    assert all("output" not in record for record in records)


def test_primary_retries_are_bounded_before_single_fallback(tmp_path: Path) -> None:
    cfg = _config(tmp_path, provider_retries=1)
    adapter = OpenCodeAdapter(cfg)
    responses = [
        types.SimpleNamespace(returncode=1, stdout="temporary provider failure"),
        types.SimpleNamespace(returncode=1, stdout="temporary provider failure"),
        types.SimpleNamespace(returncode=0, stdout="ok"),
    ]

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, side_effect=responses) as runner:
        result = adapter.invoke("planner", "Plan one task", cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == 3
    attempts = result.context["provider_attempts"]
    assert [(item["primary"], item["retry"]) for item in attempts] == [
        (True, 0),
        (True, 1),
        (False, 0),
    ]


def test_context_budget_failure_skips_futile_retries_and_uses_larger_fallback(
    tmp_path: Path,
) -> None:
    cfg = _config(
        tmp_path,
        provider_retries=3,
        primary_context=1000,
        fallback_context=10000,
    )
    adapter = OpenCodeAdapter(cfg)
    successful = types.SimpleNamespace(returncode=0, stdout="ok")

    target = "converge_orchestrator.opencode.ExecutionSandbox.run"
    with patch(target, return_value=successful) as runner:
        result = adapter.invoke("planner", "x" * 6000, cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == 1
    attempts = result.context["provider_attempts"]
    assert attempts[0]["returncode"] == 78
    assert attempts[0]["failure_kind"] == "context_budget"
    assert attempts[1]["model_profile"] == "fallback"
    assert attempts[1]["ok"] is True


def test_real_executor_process_death_retries_same_prompt_and_model(tmp_path: Path) -> None:
    cfg = _config(tmp_path, provider_retries=1)
    cfg.opencode_binary = sys.executable
    adapter = OpenCodeAdapter(cfg)
    executor = cfg.repo_path / "run"
    executor.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "state_dir = Path(os.environ['OPENCODE_CONFIG']).parent",
                "ledger = state_dir / 'executor-process-attempts.jsonl'",
                "existing = ledger.read_text(encoding='utf-8').splitlines() if ledger.exists() else []",
                "record = {'argv': sys.argv[1:], 'prompt': sys.argv[-1]}",
                "with ledger.open('a', encoding='utf-8') as fh:",
                "    fh.write(json.dumps(record, sort_keys=True) + '\\n')",
                "if not existing:",
                "    os._exit(91)",
                "print('{\"task\": \"recovered\"}')",
                "",
            ]
        ),
        encoding="utf-8",
    )

    prompt = "Plan one immutable target"
    result = adapter.invoke("planner", prompt, cfg.repo_path)

    assert result.ok is True
    attempts = result.context["provider_attempts"]
    assert [(item["returncode"], item["failure_kind"], item["ok"]) for item in attempts] == [
        (91, "process_failure", False),
        (0, None, True),
    ]
    assert result.context["fallback_used"] is False
    assert result.context["selected_model"] == "openwebui/primary-model"

    process_records = [
        json.loads(line)
        for line in (cfg.state_dir / "executor-process-attempts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(process_records) == 2
    assert [item["prompt"] for item in process_records] == [prompt, prompt]
    for record in process_records:
        argv = record["argv"]
        assert argv[argv.index("--model") + 1] == "openwebui/primary-model"
        assert "--continue" not in argv
        assert "--session" not in argv

    health = [
        json.loads(line)
        for line in (cfg.state_dir / "provider-health.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(health) == 2
    assert [item["returncode"] for item in health] == [91, 0]
    assert all(item["model"] == "openwebui/primary-model" for item in health)
    assert all("output" not in item for item in health)
