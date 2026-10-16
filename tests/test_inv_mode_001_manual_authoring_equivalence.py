"""RED evidence for INV-MODE-001: the manual 'edit' authoring path must persist the
same ``task.json`` Task Envelope evidence artifact that the autonomous planner paths write.

INV-MODE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L157, [4. Global invariants])
requires manual and autonomous authoring to use equivalent acceptance, compliance, review,
risk, integration, and evidence semantics after a candidate enters verification.

Evidence-backed gap this test pins down:

* ``workflow.human_gate``'s ``action == "edit"`` branch (src/converge_orchestrator/workflow.py
  lines ~1221-1249) returns the replacement Task Envelope into state without ever calling
  ``EvidenceStore.write_json(run_id, task.id, "task.json", ...)``.
* Every autonomous planner path does persist it:
  ``workflow.plan`` (workflow.py:592), ``graph.plan`` (graph.py:285), and the targeting
  planner (targeting.py:450).
* ``acceptance.py`` lists ``task.json`` in ``_REQUIRED_TASK_ARTIFACTS`` (acceptance.py:14-22)
  and ``_task_bundle_checks`` verifies its ``id``/``requirement_ids`` (acceptance.py:228-240).

So a manually authored candidate is not subject to the same evidence/acceptance semantics once
it enters verification. The test drives ``human_gate`` with a patched ``interrupt`` returning an
``edit`` decision and asserts the durable ``task.json`` exists for the edited Task Envelope id
with matching ``requirement_ids``. It fails RED today with the literal marker
``MODE001_EVIDENCE_DIVERGENCE`` and turns GREEN once the edit branch persists the artifact.

No network, model, subprocess, or git access is exercised: the config loader is patched to a
fixed ``ProjectConfig`` and the state carries no branch/worktree/pr, so
``_discard_current_workspace`` performs no external work.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.workflow import human_gate

_FAILURE_MARKER = "MODE001_EVIDENCE_DIVERGENCE"


def _edited_task() -> dict:
    return {
        "id": "INV-MODE-001-0038",
        "requirement_ids": ["INV-MODE-001"],
        "title": "Persist edited Task Envelope evidence in the manual authoring path",
        "objective": (
            "Write the same task.json evidence artifact the autonomous planner paths write."
        ),
        "allowed_paths": ["src/**", "tests/**"],
        "acceptance": ["task.json evidence is persisted for the edited Task Envelope id"],
    }


def test_human_gate_edit_persists_task_envelope_evidence(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        repo_path=tmp_path / "repository",
        requirements_path=tmp_path / "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md",
        state_dir=tmp_path / "state",
        agents={},
        require_spec_read_only=False,
    )
    run_id = "run-mode-001"
    edited_task = _edited_task()
    state = {
        "config_path": str(tmp_path / "converge.yaml"),
        "run_id": run_id,
        "task": {
            "id": "INV-MODE-001-0037",
            "requirement_ids": ["INV-MODE-001"],
            "title": "Prior task",
            "objective": "Prior objective",
        },
        "requirements": [
            {
                "id": "INV-MODE-001",
                "statement": (
                    "Manual and autonomous authoring SHALL use equivalent acceptance, "
                    "compliance, review, risk, integration, and evidence semantics after a "
                    "candidate enters verification."
                ),
                "source": "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L157",
                "severity": "mandatory",
                "status": "unverified",
                "evidence": [],
            }
        ],
        "risk_flags": [],
        "human_decisions": [],
        "worktree": None,
        "branch": None,
        "pr": None,
        "review_result": None,
    }

    with (
        patch(
            "converge_orchestrator.workflow._human_kind",
            return_value="repair_replan_budget",
        ),
        patch(
            "converge_orchestrator.workflow.interrupt",
            return_value={"action": "edit", "task": edited_task},
        ),
        patch("converge_orchestrator.workflow.load_config", return_value=cfg),
    ):
        result = human_gate(state)  # type: ignore[arg-type]

    # The manual edit branch must still accept the replacement envelope and route to prepare.
    assert result["status"] == "human_edit"
    assert result["task"]["id"] == edited_task["id"]

    # Equivalent evidence semantics: the durable task.json must exist for the edited Task
    # Envelope id and be bound to the same requirement ids the planners bind.
    task_path = cfg.state_dir / "evidence" / run_id / edited_task["id"] / "task.json"
    assert task_path.is_file(), (
        f"{_FAILURE_MARKER}: manual 'edit' authoring returned the replacement Task Envelope "
        "into state without persisting task.json (expected at "
        f"{task_path}); the autonomous planner paths (workflow.plan, graph.plan, "
        "targeting planner) all write this artifact, so a manually authored candidate is "
        "not subject to equivalent acceptance/evidence semantics after entering "
        "verification."
    )

    persisted = json.loads(task_path.read_text(encoding="utf-8"))
    assert persisted.get("id") == edited_task["id"], (
        f"{_FAILURE_MARKER}: persisted task.json id {persisted.get('id')!r} does not match "
        f"the edited Task Envelope id {edited_task['id']!r}"
    )
    assert persisted.get("requirement_ids") == edited_task["requirement_ids"], (
        f"{_FAILURE_MARKER}: persisted task.json requirement_ids "
        f"{persisted.get('requirement_ids')!r} do not match the edited Task Envelope "
        f"requirement_ids {edited_task['requirement_ids']!r}"
    )
