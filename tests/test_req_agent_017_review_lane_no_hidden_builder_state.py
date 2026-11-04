"""Pin REQ-AGENT-017: configured review lanes run isolated sessions with no hidden Builder
conversation state.

REQ-AGENT-017 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L252
[7. Deterministic architecture and agent roles]): A configured review lane SHALL NOT rely on
hidden Builder conversation state.

Deterministic offline assertions lock the OpenCode invocation envelope, the ExecutionSandbox
contract, and the rendered prompt content for every configured review role. No network, model
provider, git command, subprocess call, or filesystem mutation outside the test ``tmp_path`` is
performed.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import (
    _REVIEW_AGENT_ROLES,
    AgentConfig,
    ComplianceSnapshot,
    ProjectConfig,
    Requirement,
    ReviewResult,
    TaskEnvelope,
)
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.prompts import reviewer_prompt

_MARKER = (
    "REQ-AGENT-017 review lane must not rely on hidden Builder conversation state "
    "(marker: AGENT017_REVIEW_LANE_HIDDEN_STATE)"
)
_REVIEW_ROLES = sorted(_REVIEW_AGENT_ROLES)
# Substrings that belong to Builder/TDD-RED prompts or hidden continuity state, not review input.
_BUILDER_MARKERS = (
    "Implement the task below in this isolated git worktree.",
    "TDD RED EVIDENCE:",
    "PRIOR RED ATTEMPT EVIDENCE:",
    "frozen RED test",
)


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    req = tmp_path / "architecture.md"
    req.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=req,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        review_roles=list(_REVIEW_ROLES),
        agents={
            role: AgentConfig(agent=f"converge-{role.replace('_', '-')}")
            for role in _REVIEW_ROLES
        },
    )


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="REQ-AGENT-017-1",
        requirement_ids=["REQ-AGENT-017"],
        title="Review lane isolation",
        objective="Prove every configured review lane uses a fresh isolated session.",
    )


def _requirements() -> list[Requirement]:
    return [
        Requirement(
            id="REQ-AGENT-017",
            statement="Review lanes shall not rely on hidden Builder conversation state.",
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L252",
        )
    ]


def test_req_agent_017_review_lane_is_fresh_isolated_session(tmp_path: Path) -> None:
    """Every configured review-lane invocation is fresh, stateless, and Builder-state-free."""
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    prompt = reviewer_prompt(_task(), "diff text", _requirements(), ComplianceSnapshot())
    payload = ReviewResult(verdict="pass", findings=[], confidence=0.9).model_dump_json()

    def fake_run(command, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=payload)

    with patch(
        "converge_orchestrator.opencode.ExecutionSandbox.run", side_effect=fake_run
    ) as runner:
        result = adapter.invoke("reviewer", prompt, cfg.repo_path)

    assert result.ok, _MARKER
    assert len(runner.call_args_list) == len(_REVIEW_ROLES), _MARKER
    for call in runner.call_args_list:
        cmd, kwargs = call.args[0], call.kwargs
        assert isinstance(cmd, list), _MARKER
        assert "--continue" not in cmd and "--session" not in cmd, _MARKER
        assert kwargs.get("include_state") is False, _MARKER
        assert kwargs.get("writable_cwd") is False, _MARKER
        assert kwargs.get("agent_role") in _REVIEW_AGENT_ROLES, _MARKER
        rendered = kwargs.get("input")
        assert rendered is not None, _MARKER
        for marker in _BUILDER_MARKERS:
            assert marker not in rendered, f"{_MARKER}: review input contains {marker!r}"

    state_dir = cfg.state_dir
    assert state_dir is not None, _MARKER
    ledger = state_dir / "context-usage.jsonl"
    assert ledger.exists(), _MARKER
    for line in ledger.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        assert record.get("session_mode") == "fresh", (
            f"{_MARKER}: context ledger session_mode is {record.get('session_mode')!r}, not fresh"
        )
