"""Pin REQ-AGENT-014: repository scout reports bounded evidence for the exact base revision.

REQ-AGENT-014 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L247
[7. Deterministic architecture and agent roles]): Repository reconnaissance SHALL report bounded
evidence about the exact base revision.

These deterministic assertions lock the scout prompt contract, the bounded ``base_commit_evidence``
field on ``RepoScoutPayload``, its persistence to ``repo-scout.json``, and the missing-evidence
warning. No network, model provider, git command, subprocess call, or filesystem mutation outside
the test ``tmp_path`` is performed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.graph import _scout_prompt, scout
from converge_orchestrator.models import AgentResult, ProjectConfig, Requirement

_MARKER = "REQ-AGENT-014 recon report must include bounded base-revision evidence"


def _require(condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{_MARKER}: {detail}".strip())


def _requirement() -> Requirement:
    return Requirement(
        id="REQ-AGENT-014",
        statement=(
            "Repository reconnaissance SHALL report bounded evidence "
            "about the exact base revision."
        ),
        source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:247",
    )


def _config(tmp_path: Path, *, include_scout: bool = True) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain modular.\n", encoding="utf-8")
    agents = {
        "planner": {"agent": "converge-planner", "model": "openai/planner"},
    }
    if include_scout:
        agents["scout"] = {"agent": "converge-scout", "model": "openai/scout"}
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        state_dir=tmp_path / "state",
        require_spec_read_only=False,
        agents=agents,
    )


def _state(tmp_path: Path) -> dict:
    return {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": "run-1",
        "thread_id": "thread-1",
        "requirements_hash": "sha",
        "requirements": [_requirement().model_dump(mode="json")],
        "baseline": {"deterministic": {"stack": "python"}},
        "compliance": {"entries": {}, "mandatory_regressions": 0},
        "iteration": 0,
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "approved_risk_flags": [],
        "status": "spec_ok",
    }


def test_scout_prompt_requires_bounded_base_revision_evidence() -> None:
    """The scout prompt schema and instructions require bounded base-revision evidence."""
    prompt = _scout_prompt([_requirement()], "main", "abc1234def")

    _require('"base_commit_evidence":' in prompt, "prompt schema omits base_commit_evidence")
    _require(
        "observed base commit" in prompt,
        "prompt does not ask for the observed base commit",
    )
    _require(
        "git log" in prompt and "--oneline" in prompt,
        "prompt does not ask for oneline git-log evidence",
    )


def test_scout_payload_carries_bounded_base_commit_evidence(tmp_path: Path) -> None:
    """The normalized scout payload keeps at most 8 unique evidence items <=200 chars."""
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    long_item = "x" * 250
    evidence = [f"{i:07d} headline for commit {i}" for i in range(10)] + [long_item]
    output = {
        "summary": "Python service.",
        "stacks": ["python"],
        "key_paths": ["src/domain/service.py"],
        "test_paths": ["tests/test_service.py"],
        "architecture_notes": [],
        "risk_notes": [],
        "requirement_hints": {},
        "uncertainties": [],
        "base_commit_evidence": evidence,
    }

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.graph.update_base", return_value="abc1234"),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=AgentResult(
                role="scout",
                ok=True,
                output=json.dumps(output),
                returncode=0,
            ),
        ),
    ):
        result = scout(state)

    snapshot = result["baseline"]["repo_scout"]
    _require(
        "base_commit_evidence" in snapshot,
        "agent snapshot missing base_commit_evidence",
    )
    bounded = snapshot["base_commit_evidence"]
    _require(isinstance(bounded, list), "base_commit_evidence is not a list")
    _require(len(bounded) <= 8, f"base_commit_evidence has {len(bounded)} items (>8)")
    _require(
        all(len(item) <= 200 for item in bounded),
        "base_commit_evidence item exceeds 200 characters",
    )

    artifact = cfg.state_dir / "evidence" / "run-1" / "run" / "repo-scout.json"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    _require(
        "base_commit_evidence" in persisted,
        "repo-scout.json artifact missing base_commit_evidence",
    )
    _require(
        len(persisted["base_commit_evidence"]) <= 8,
        "persisted base_commit_evidence exceeds 8 items",
    )
    _require(
        all(len(item) <= 200 for item in persisted["base_commit_evidence"]),
        "persisted base_commit_evidence item exceeds 200 characters",
    )


def test_scout_warns_when_base_commit_evidence_missing(tmp_path: Path) -> None:
    """A scout report that omits base_commit_evidence produces a deterministic warning."""
    cfg = _config(tmp_path)
    state = _state(tmp_path)
    output = {
        "summary": "Python service.",
        "stacks": ["python"],
        "key_paths": ["src/domain/service.py"],
        "test_paths": ["tests/test_service.py"],
        "architecture_notes": [],
        "risk_notes": [],
        "requirement_hints": {},
        "uncertainties": [],
    }

    with (
        patch("converge_orchestrator.graph.load_config", return_value=cfg),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
        patch("converge_orchestrator.graph.update_base", return_value="abc1234"),
        patch(
            "converge_orchestrator.graph.OpenCodeAdapter.invoke",
            return_value=AgentResult(
                role="scout",
                ok=True,
                output=json.dumps(output),
                returncode=0,
            ),
        ),
    ):
        result = scout(state)

    snapshot = result["baseline"]["repo_scout"]
    _require(
        any("base_commit_evidence" in warning for warning in snapshot["warnings"]),
        "missing base_commit_evidence did not produce a warning",
    )

