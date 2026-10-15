"""Lock INV-INTEGRITY-002: a previously evidenced mandatory PASS must not regress.

INV-INTEGRITY-002 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L151 [4. Global invariants]):
A previously evidenced mandatory PASS SHALL NOT regress as a side effect of a candidate proven
unrelated to the evidence supporting that PASS, while inability to prove continued evidence
applicability SHALL NOT be treated as proof that the PASS survived.

These deterministic tests pin two enforcement surfaces:
``ComplianceEngine.compare_mandatory_regressions`` — a mandatory PASS preserved across an
unrelated candidate yields zero mandatory regressions, while the same PASS dropping to any
non-PASS status (UNVERIFIED/PARTIAL/FAIL/BLOCKED) is counted as a regression — and
``quality.run_scope_gate``, whose convergence block fails the required ``diff_scope`` gate when
the PASS cannot be re-proven on the candidate, even when the candidate's own target requirement
improves and the diff stays in scope; the gate passes when the PASS is preserved.

No network, models, subprocesses, or git: verifiers and git evidence are deterministic fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from converge_orchestrator.compliance import ComplianceEngine
from converge_orchestrator.models import (
    ComplianceEntry,
    ComplianceSnapshot,
    Contract,
    ContractSource,
    GateResult,
    ProjectConfig,
    QualityGate,
    Requirement,
    RequirementStatus,
    RequirementVerification,
    TaskEnvelope,
)
from converge_orchestrator.quality import run_scope_gate

_INV_ID = "INV-INTEGRITY-002"
_TASK_ID = "TASK-001"
_SPEC = "CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md"
_TEST_FILE = "tests/test_inv_integrity_002_monotonic_pass.py"


def _contract() -> Contract:
    return Contract(
        source=ContractSource(path=_SPEC, sha256="0" * 64),
        requirements=[
            Requirement(
                id=_INV_ID,
                statement="A previously evidenced mandatory PASS must not regress.",
                source=f"{_SPEC}:L151",
            ),
            Requirement(
                id=_TASK_ID,
                statement="The candidate task's own target requirement.",
                source=f"{_SPEC}:L200",
            ),
        ],
    )


def _snapshot(
    statuses: dict[str, RequirementStatus],
    evidence: dict[str, list[str]] | None = None,
) -> ComplianceSnapshot:
    evidence = evidence or {}
    return ComplianceSnapshot(
        entries={
            requirement_id: ComplianceEntry(
                requirement_id=requirement_id,
                status=status,
                evidence=list(evidence.get(requirement_id, [])),
            )
            for requirement_id, status in statuses.items()
        }
    )


def _mandatory_regressions(baseline: ComplianceSnapshot, candidate: ComplianceSnapshot) -> int:
    return ComplianceEngine.compare_mandatory_regressions(
        baseline, candidate, _contract().requirements
    )


def test_preserved_mandatory_pass_yields_zero_mandatory_regressions() -> None:
    """An evidenced mandatory PASS preserved across an unrelated candidate does not regress."""
    baseline = _snapshot(
        {_INV_ID: RequirementStatus.PASS, _TASK_ID: RequirementStatus.UNVERIFIED},
        evidence={_INV_ID: ["ci-run-101"]},
    )
    candidate = _snapshot(
        {_INV_ID: RequirementStatus.PASS, _TASK_ID: RequirementStatus.PASS},
        evidence={_INV_ID: ["ci-run-202"], _TASK_ID: [_TEST_FILE]},
    )
    assert _mandatory_regressions(baseline, candidate) == 0
    assert candidate.entries[_INV_ID].status == RequirementStatus.PASS
    assert candidate.entries[_INV_ID].evidence


@pytest.mark.parametrize(
    "candidate_status",
    [
        RequirementStatus.UNVERIFIED,
        RequirementStatus.PARTIAL,
        RequirementStatus.FAIL,
        RequirementStatus.BLOCKED,
    ],
    ids=["unverified", "partial", "fail", "blocked"],
)
def test_unreprovable_mandatory_pass_is_counted_as_regression(
    candidate_status: RequirementStatus,
) -> None:
    """Inability to prove continued evidence applicability is not treated as survival."""
    baseline = _snapshot({_INV_ID: RequirementStatus.PASS}, evidence={_INV_ID: ["ci-run-101"]})
    candidate = _snapshot({_INV_ID: candidate_status})
    assert _mandatory_regressions(baseline, candidate) == 1


def test_regression_count_tracks_every_lost_mandatory_pass() -> None:
    """Each lost mandatory PASS is a regression; preserved ones are not."""
    requirements = _contract().requirements + [
        Requirement(id="INV-003", statement="Third mandatory rule.", source=f"{_SPEC}:L152")
    ]
    ids = [requirement.id for requirement in requirements]
    baseline = _snapshot({requirement_id: RequirementStatus.PASS for requirement_id in ids})
    partially_lost = _snapshot(
        {
            _INV_ID: RequirementStatus.PASS,
            _TASK_ID: RequirementStatus.FAIL,
            "INV-003": RequirementStatus.UNVERIFIED,
        }
    )
    assert (
        ComplianceEngine.compare_mandatory_regressions(baseline, partially_lost, requirements) == 2
    )
    all_lost = _snapshot({requirement_id: RequirementStatus.FAIL for requirement_id in ids})
    assert ComplianceEngine.compare_mandatory_regressions(baseline, all_lost, requirements) == 3


def test_non_mandatory_and_improving_transitions_are_not_regressions() -> None:
    """Recommended losses and improvements toward PASS are never mandatory regressions."""
    requirements = _contract().requirements + [
        Requirement(
            id="RECO-001",
            statement="Recommended rule.",
            source=f"{_SPEC}:L210",
            severity="recommended",
        )
    ]
    baseline = _snapshot({"RECO-001": RequirementStatus.PASS, _TASK_ID: RequirementStatus.FAIL})
    candidate = _snapshot(
        {"RECO-001": RequirementStatus.FAIL, _TASK_ID: RequirementStatus.PASS},
        evidence={_TASK_ID: ["ci-run-202"]},
    )
    assert ComplianceEngine.compare_mandatory_regressions(baseline, candidate, requirements) == 0


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="INV-INTEGRITY-002-0038",
        requirement_ids=[_TASK_ID],
        title="Lock INV-INTEGRITY-002 monotonic mandatory-PASS invariant",
        objective="Pin INV-INTEGRITY-002 on the convergence surfaces.",
        allowed_paths=["tests/**"],
        max_diff_lines=260,
        change_kind="test_only",
    )


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / _SPEC,
        agents={},
        requirement_verifiers={
            _TASK_ID: [QualityGate(name="verify-task-target", command="pytest -q tests")]
        },
    )


def _scope_gate(tmp_path: Path, inv_candidate_status: RequirementStatus) -> GateResult:
    """Drive run_scope_gate over deterministic verifier fixtures (no git, subprocess, models)."""
    baseline_results = [
        RequirementVerification(
            requirement_id=_INV_ID, status=RequirementStatus.PASS, evidence=["ci-run-101"]
        ),
        RequirementVerification(requirement_id=_TASK_ID, status=RequirementStatus.UNVERIFIED),
    ]
    candidate_results = [
        RequirementVerification(
            requirement_id=_INV_ID,
            status=inv_candidate_status,
            evidence=["ci-run-202"] if inv_candidate_status is RequirementStatus.PASS else [],
        ),
        RequirementVerification(
            requirement_id=_TASK_ID, status=RequirementStatus.PASS, evidence=[_TEST_FILE]
        ),
    ]
    with (
        patch("converge_orchestrator.quality.compile_contract", return_value=_contract()),
        patch(
            "converge_orchestrator.quality._baseline_requirement_verifiers",
            return_value=(baseline_results, False),
        ),
        patch(
            "converge_orchestrator.quality.run_requirement_verifiers",
            return_value=candidate_results,
        ),
        patch("converge_orchestrator.quality.changed_files", return_value=[_TEST_FILE]),
        patch("converge_orchestrator.quality.diff_line_count", return_value=120),
    ):
        return run_scope_gate(_config(tmp_path), tmp_path, _task())


def test_scope_gate_passes_when_evidenced_mandatory_pass_is_preserved(tmp_path: Path) -> None:
    """Positive control: the gate is not uniformly blocking when the PASS survives."""
    result = _scope_gate(tmp_path, RequirementStatus.PASS)
    assert result.name == "diff_scope"
    assert result.ok
    assert result.required
    convergence = json.loads(result.output)["convergence"]
    assert convergence["mode"] == "deterministic_requirement_verifiers"
    assert convergence["mandatory_regressions"] == 0
    assert convergence["target_improved"] == [_TASK_ID]


@pytest.mark.parametrize(
    "inv_candidate_status",
    [
        RequirementStatus.UNVERIFIED,
        RequirementStatus.PARTIAL,
        RequirementStatus.FAIL,
        RequirementStatus.BLOCKED,
    ],
    ids=["unverified", "partial", "fail", "blocked"],
)
def test_scope_gate_blocks_when_mandatory_pass_cannot_be_reproven(
    tmp_path: Path, inv_candidate_status: RequirementStatus
) -> None:
    """A non-PASS candidate status is a regression even with green target progress and scope."""
    result = _scope_gate(tmp_path, inv_candidate_status)
    assert result.name == "diff_scope"
    assert not result.ok
    assert result.required
    details = json.loads(result.output)
    assert details["changed_files"] == [_TEST_FILE]
    assert details["allowed_paths"] == ["tests/**"]
    convergence = details["convergence"]
    assert convergence["mandatory_regressions"] == 1
    assert convergence["target_improved"] == [_TASK_ID]
    assert convergence["baseline"][_INV_ID] == "pass"
    assert convergence["candidate"][_INV_ID] == inv_candidate_status.value
