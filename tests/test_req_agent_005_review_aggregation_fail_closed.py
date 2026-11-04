"""Pin REQ-AGENT-005: a timed-out or unavailable mandatory review lane makes the
aggregate review non-PASS.

REQ-AGENT-005 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L253
[7. Deterministic architecture and agent roles]): Review aggregation SHALL be deterministic,
and any rejecting, malformed, timed-out, unavailable, or failed mandatory review lane SHALL
make the aggregate review non-PASS.

Deterministic offline assertions lock the fan-out exception fail-closed path. One mandatory
lane is forced to raise ``subprocess.TimeoutExpired`` (timed-out) or ``OSError``
(unavailable) inside ``ExecutionSandbox.run``; the orchestrator normalizes the fault to an
attributed reject lane and the aggregate verdict is non-PASS while every configured role
remains present in the reviewers map. No network, model, git, subprocess, or filesystem
mutation is performed.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from converge_orchestrator.models import AgentConfig, ProjectConfig, ReviewResult
from converge_orchestrator.opencode import OpenCodeAdapter

_ROLES = [
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
]


def _config(tmp_path: Path) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    req = tmp_path / "requirements.md"
    req.write_text("System must remain correct.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=repo,
        requirements_path=req,
        require_spec_read_only=False,
        review_roles=_ROLES,
        max_parallel_reviews=3,
        agents={
            role: AgentConfig(
                agent=f"converge-{role.replace('_', '-')}",
                model="openai/gpt-4o-mini",
            )
            for role in _ROLES
        },
    )


def _agent_id(role: str) -> str:
    return f"converge-{role.replace('_', '-')}"


@pytest.mark.parametrize(
    "exception_factory, exception_type",
    [
        (lambda: subprocess.TimeoutExpired("opencode", 1800), "TimeoutExpired"),
        (lambda: OSError("sandbox unavailable"), "OSError"),
    ],
    ids=["timed_out", "unavailable"],
)
def test_req_agent_005_mandatory_lane_exception_makes_aggregate_non_pass(
    tmp_path: Path,
    exception_factory: Any,
    exception_type: str,
) -> None:
    """A mandatory review lane that raises a subprocess/IO fault becomes an attributed reject lane.

    The healthy lanes return valid pass reviews. The failing lane raises the supplied
    exception from ``ExecutionSandbox.run``. The aggregate ``ReviewResult`` must then be
    ``reject``, the reviewers map must keep every configured role, and the failing lane's
    finding must cite the exception type.
    """
    failing_role = "security_reviewer"
    cfg = _config(tmp_path)
    adapter = OpenCodeAdapter(cfg)
    pass_payload = json.dumps({"verdict": "pass", "findings": [], "confidence": 0.9})

    def fake_run(command, **kwargs):
        del kwargs
        agent_id = command[command.index("--agent") + 1]
        if agent_id == _agent_id(failing_role):
            raise exception_factory()
        return types.SimpleNamespace(returncode=0, stdout=pass_payload)

    with (
        patch(
            "converge_orchestrator.opencode.ExecutionSandbox.run",
            side_effect=fake_run,
        ) as runner,
        patch(
            "converge_orchestrator.opencode.materialize_opencode_config",
            return_value=tmp_path / "opencode.generated.json",
        ),
        patch(
            "converge_orchestrator.opencode.materialize_managed_skills",
            return_value=tmp_path / "managed",
        ),
        patch("converge_orchestrator.opencode.append_context_ledger"),
        patch.object(adapter, "_append_provider_health"),
    ):
        result = adapter.invoke("reviewer", "Review this diff", cfg.repo_path)

    assert result.ok is True
    assert runner.call_count == len(_ROLES)

    aggregate = ReviewResult.model_validate_json(result.output)
    assert aggregate.verdict == "reject"
    assert set(aggregate.reviewers.keys()) == set(_ROLES)
    assert aggregate.reviewers[failing_role] == "reject"
    for role in _ROLES:
        if role != failing_role:
            assert aggregate.reviewers[role] == "pass"

    failing_findings = [
        finding for finding in aggregate.findings if finding.reviewer == failing_role
    ]
    assert len(failing_findings) == 1
    assert exception_type in failing_findings[0].reason
    assert aggregate.confidence == 0.9
