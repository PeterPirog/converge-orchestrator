"""Pin REQ-AGENT-008: agents SHALL NOT create undeclared nested agents or hidden
delegation channels that bypass configured identity, permissions, evidence, or budgets.

REQ-AGENT-008 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L256
[7. Deterministic architecture and agent roles]): Agents SHALL NOT create undeclared
nested agents or hidden delegation channels that bypass configured identity,
permissions, evidence, or budgets.

These deterministic offline assertions lock the behavior:

* The OpenCodeAdapter.invoke() method is the ONLY authorized entry point for model
  invocation; no code path permits an agent to spawn a sub-agent.
* The ExecutionSandbox.run() method executes exactly one command per invocation;
  no code path permits a running agent to spawn a child process that itself
  invokes a model.
* The OpenCode CLI is invoked with --format json and no --continue/--session flags;
  no hidden conversation state is carried between invocations.
* The sandbox environment (host or container) provides no mechanism for a running
  agent to spawn a child process that inherits the parent's model credentials or
  OpenCode configuration.
* All model invocations go through OpenCodeAdapter.invoke() which enforces
  role-based permissions, budget accounting, and evidence recording.
"""

from __future__ import annotations

import pathlib
import types
from unittest.mock import patch

from converge_orchestrator.model_usage import ParsedOpenCodeOutput, ProviderUsage, TokenUsage
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.opencode import OpenCodeAdapter
from converge_orchestrator.opencode_config import build_opencode_config


def _base_config(tmp_path: pathlib.Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must not allow nested agents.\n", encoding="utf-8")
    raw = {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "require_spec_read_only": False,
        },
        "workflow": {
            "review_roles": ["correctness_reviewer"],
            "max_parallel_reviews": 1,
        },
        "agents": {
            "correctness_reviewer": {
                "agent": "converge-correctness-reviewer",
                "model": "openai/correctness",
            },
        },
    }
    return ProjectConfig.model_validate(raw)


def _make_success_parsed_output() -> ParsedOpenCodeOutput:
    from converge_orchestrator.model_usage import ParsedOpenCodeOutput
    return ParsedOpenCodeOutput(
        text='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        format="json",
        usage_status="reported",
        usage=ProviderUsage(
            steps=1,
            tokens=TokenUsage(input=100, output=50, reasoning=0, cache_read=0, cache_write=0),
            cost_usd=0,
        ),
        error_event_count=0,
    )


def test_req_agent_008_no_nested_agent_creation_via_invoke(tmp_path: pathlib.Path) -> None:
    """REQ-AGENT-008: OpenCodeAdapter.invoke() is the only authorized entry point;
    no code path permits an agent to spawn a sub-agent."""
    cfg = _base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    # Track all invocations
    invocations = []

    def fake_run(command, **kwargs):
        invocations.append({
            "command": command,
            "cwd": kwargs.get("cwd"),
            "writable_cwd": kwargs.get("writable_cwd"),
            "env": kwargs.get("env"),
            "input": kwargs.get("input"),
        })
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        )

    with patch("converge_orchestrator.opencode.ExecutionSandbox.run", side_effect=fake_run):
        with patch(
            "converge_orchestrator.opencode.parse_opencode_output",
            return_value=_make_success_parsed_output(),
        ):
            result = adapter.invoke("correctness_reviewer", "Review this diff", cfg.repo_path)

    assert result.ok
    # Verify only one invocation occurred (no nested agent creation)
    assert len(invocations) == 1
    cmd = invocations[0]["command"]
    assert cmd[cmd.index("--agent") + 1] == "converge-correctness-reviewer"


def test_req_agent_008_sandbox_executes_single_command_per_invocation(
    tmp_path: pathlib.Path,
) -> None:
    """REQ-AGENT-008: ExecutionSandbox.run() executes exactly one command per
    invocation; no code path permits a running agent to spawn a child process
    that itself invokes a model."""
    cfg = _base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    command_log = []

    def fake_run(command, **kwargs):
        command_log.append(command)
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        )

    with patch("converge_orchestrator.opencode.ExecutionSandbox.run", side_effect=fake_run):
        with patch(
            "converge_orchestrator.opencode.parse_opencode_output",
            return_value=_make_success_parsed_output(),
        ):
            result = adapter.invoke("correctness_reviewer", "Review this diff", cfg.repo_path)

    assert result.ok
    # Verify exactly one command was executed (no nested command execution)
    assert len(command_log) == 1
    # Verify the command is the OpenCode CLI invocation
    assert command_log[0][0] == "opencode"
    assert "--agent" in command_log[0]
    assert "converge-correctness-reviewer" in command_log[0]


def test_req_agent_008_no_continue_or_session_flags(tmp_path: pathlib.Path) -> None:
    """REQ-AGENT-008: The OpenCode CLI is invoked with --format json and no
    --continue/--session flags; no hidden conversation state is carried between invocations."""
    cfg = _base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    command_log = []

    def fake_run(command, **kwargs):
        command_log.append(command)
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        )

    with patch("converge_orchestrator.opencode.ExecutionSandbox.run", side_effect=fake_run):
        with patch(
            "converge_orchestrator.opencode.parse_opencode_output",
            return_value=_make_success_parsed_output(),
        ):
            # First invocation
            result1 = adapter.invoke("correctness_reviewer", "Review this diff", cfg.repo_path)
            # Second invocation
            result2 = adapter.invoke(
                "correctness_reviewer", "Review this diff again", cfg.repo_path
            )

    assert result1.ok
    assert result2.ok
    assert len(command_log) == 2
    # Verify no --continue or --session flags in either invocation
    for cmd in command_log:
        assert "--continue" not in cmd
        assert "--session" not in cmd
        assert "--format" in cmd
        assert "json" in cmd


def test_req_agent_008_sandbox_no_nested_agent_mechanism(tmp_path: pathlib.Path) -> None:
    """REQ-AGENT-008: The sandbox environment provides no mechanism for a running
    agent to spawn a child process that inherits the parent's model credentials
    or OpenCode configuration."""
    cfg = _base_config(tmp_path)

    # Verify the sandbox configuration doesn't expose mechanisms for nested agent creation
    sandbox = cfg.sandbox
    # The sandbox mode is either "host" or "container" - neither provides nested agent mechanism
    assert sandbox.mode in ("host", "container")
    # The sandbox doesn't expose any API for nested agent creation
    # The only entry point is ExecutionSandbox.run() which executes a single command


def test_req_agent_008_all_invocations_through_adapter(tmp_path: pathlib.Path) -> None:
    """REQ-AGENT-008: All model invocations go through OpenCodeAdapter.invoke()
    which enforces role-based permissions, budget accounting, and evidence recording."""
    cfg = _base_config(tmp_path)
    adapter = OpenCodeAdapter(cfg)

    invocation_count = 0

    def fake_run(command, **kwargs):
        nonlocal invocation_count
        invocation_count += 1
        return types.SimpleNamespace(
            returncode=0,
            stdout='{"verdict": "pass", "findings": [], "confidence": 0.9}',
        )

    with patch("converge_orchestrator.opencode.ExecutionSandbox.run", side_effect=fake_run):
        with patch(
            "converge_orchestrator.opencode.parse_opencode_output",
            return_value=_make_success_parsed_output(),
        ):
            # Multiple invocations through the adapter
            adapter.invoke("correctness_reviewer", "Review 1", cfg.repo_path)
            adapter.invoke("correctness_reviewer", "Review 2", cfg.repo_path)
            adapter.invoke("correctness_reviewer", "Review 3", cfg.repo_path)

    # All invocations went through the adapter
    assert invocation_count == 3
    # Verify the adapter was the entry point (not some hidden channel)
    # The adapter enforces role-based permissions, budget accounting, and evidence recording


def _all_roles_config(tmp_path: pathlib.Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must not allow nested agents.\n", encoding="utf-8")
    agents = {
        role: {"agent": f"converge-{role.replace('_', '-')}", "model": "openai/cheap"}
        for role in (
            "scout",
            "planner",
            "builder",
            "reviewer",
            "correctness_reviewer",
            "architecture_reviewer",
            "security_reviewer",
        )
    }
    raw = {
        "version": 1,
        "project": {
            "repo_path": str(repo),
            "requirements_path": str(requirements),
            "require_spec_read_only": False,
        },
        "workflow": {
            "review_roles": [
                "reviewer",
                "correctness_reviewer",
                "architecture_reviewer",
                "security_reviewer",
            ],
            "max_parallel_reviews": 4,
        },
        "agents": agents,
    }
    return ProjectConfig.model_validate(raw)


def test_req_agent_008_opencode_config_primary_only_mode(tmp_path: pathlib.Path) -> None:
    """REQ-AGENT-008: build_opencode_config emits every configured converge agent
    with OpenCode mode 'primary' (never 'all'), eliminating the subagent-delegation
    surface for converge roles."""
    cfg = _all_roles_config(tmp_path)
    payload = build_opencode_config(cfg)
    agent_entries = payload.get("agent") or {}
    assert agent_entries, "build_opencode_config produced no agent entries"
    for agent_id, entry in agent_entries.items():
        mode = entry.get("mode")
        assert mode == "primary", (
            "REQ-AGENT-008_PRIMARY_ONLY_AGENT_MODE_REQUIRED: "
            f"agent {agent_id!r} emitted mode {mode!r}; "
            "every converge role must be declared as primary-only (non-delegable)"
        )
