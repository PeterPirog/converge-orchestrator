from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.context import (
    AdvisorySection,
    ContextBudgetExceeded,
    PromptEnvelope,
    build_working_memory,
    prepare_prompt,
)
from converge_orchestrator.models import ProjectConfig, Requirement, TaskEnvelope
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.prompts import contract_excerpt, reviewer_prompt


def _config(tmp_path: Path, *, context_tokens: int = 6000) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        require_spec_read_only=False,
        context_input_fraction=0.5,
        context_output_reserve_tokens=1000,
        model_profiles={
            "planner": {
                "model": "openai/test-model",
                "context_tokens": context_tokens,
            }
        },
        agents={
            "planner": {
                "agent": "converge-planner",
                "model_profile": "planner",
            }
        },
    )


def test_contract_excerpt_never_silently_drops_requirement_after_80() -> None:
    requirements = [
        Requirement(
            id=f"ARCH-{index:03d}",
            statement=f"Requirement {index}",
            source=f"spec:{index}",
        )
        for index in range(100)
    ]

    rendered = contract_excerpt(requirements)

    assert "ARCH-099" in rendered
    assert len(rendered.splitlines()) == 100


def test_reviewer_prompt_contains_complete_diff() -> None:
    requirement = Requirement(id="ARCH-001", statement="Keep boundary", source="spec:1")
    task = TaskEnvelope(
        id="ARCH-001-1",
        requirement_ids=["ARCH-001"],
        title="Boundary",
        objective="Preserve boundary",
    )
    diff_text = "prefix-marker\n" + ("x" * 40000) + "\nsuffix-marker"

    rendered = reviewer_prompt(task, diff_text, [requirement])

    assert "prefix-marker" in rendered
    assert "suffix-marker" in rendered


def test_advisory_context_is_compacted_but_core_is_preserved(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    core = "CORE:" + ("a" * 2500)
    envelope = PromptEnvelope(
        core=core,
        advisory=(AdvisorySection("large scout", "b" * 12000),),
    )

    rendered, report = prepare_prompt(cfg, "planner", envelope)

    assert rendered.startswith(core)
    assert report.budget_status == "bounded"
    assert report.advisory_truncated == ["large scout"]
    assert report.advisory_dropped == []
    assert report.estimated_input_tokens <= report.input_budget_tokens
    assert "advisory truncated by deterministic context budget" in rendered


def test_authoritative_core_over_budget_fails_without_truncation(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    with pytest.raises(ContextBudgetExceeded) as raised:
        prepare_prompt(cfg, "planner", "z" * 12000)

    assert raised.value.report.budget_status == "core_exceeded"
    assert raised.value.report.estimated_core_tokens > raised.value.report.input_budget_tokens


def test_opencode_invocation_is_fresh_and_writes_context_ledger(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    fake_result = type("Result", (), {"returncode": 0, "stdout": "ok"})()

    with patch("converge_orchestrator.opencode.run", return_value=fake_result) as runner:
        result = adapter.invoke("planner", "Plan one task", cfg.repo_path)

    assert result.ok
    command = runner.call_args.args[0]
    assert "--continue" not in command
    assert "--session" not in command
    assert result.context["session_mode"] == "fresh"
    ledger = cfg.state_dir / "context-usage.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["role"] == "planner"
    assert records[0]["session_mode"] == "fresh"
    assert records[0]["estimated_output_tokens"] == 1


def test_core_budget_failure_does_not_launch_opencode(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    with patch("converge_orchestrator.opencode.run") as runner:
        result = adapter.invoke("planner", "z" * 12000, cfg.repo_path)

    runner.assert_not_called()
    assert not result.ok
    assert result.returncode == 78
    assert result.context["budget_status"] == "core_exceeded"
    assert "refusing silent truncation" in result.output


def test_working_memory_is_bounded_and_contains_no_requirement_summaries() -> None:
    requirements = [
        {
            "id": f"ARCH-{index:03d}",
            "statement": f"Sensitive requirement statement {index}",
            "source": f"architecture.md:{index + 1}",
            "severity": "mandatory",
        }
        for index in range(100)
    ]
    entries = {
        item["id"]: {"requirement_id": item["id"], "status": "fail", "evidence": []}
        for item in requirements
    }
    state = {
        "requirements": requirements,
        "compliance": {"entries": entries},
        "iteration": 7,
        "status": "replanning",
    }

    memory = build_working_memory(state)
    serialized = json.dumps(memory)

    assert len(memory["unresolved_mandatory_requirement_ids"]) == 64
    assert memory["unresolved_ids_truncated"] is True
    assert "ARCH-099" not in memory["unresolved_mandatory_requirement_ids"]
    assert "Sensitive requirement statement" not in serialized
    assert memory["immutable_requirements_remain_authoritative"] is True
