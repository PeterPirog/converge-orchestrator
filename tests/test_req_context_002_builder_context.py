"""Pin REQ-CONTEXT-002: builder context composition.

CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L270 [8. Context and role handoff]:
Builder context SHALL contain the validated Task Envelope, exact target requirement,
applicable constraints, and deterministic test or verifier evidence relevant to the candidate.

These deterministic offline assertions lock the builder prompt as the controller-composed
bounded context artifact:

1. The full validated Task Envelope (id, title, objective, constraints, allowed_paths,
   acceptance, change_kind, tdd contract) is rendered inside the prompt.
2. Only the exact target requirement excerpt is included; non-target requirements are
   deterministically excluded.
3. For TDD-required candidates, the deterministic RED-phase evidence artifact is present.
4. For non-TDD candidates, no RED evidence or TDD narrative leaks into the builder context.
5. The builder prompt contains no cross-role narrative assertions, respecting the
   ARCH-CTRL-002 bounded-artifact prohibitions.

No network, model, provider, git command, subprocess call, or filesystem mutation is
performed.
"""

from __future__ import annotations

import json

from converge_orchestrator.models import (
    Requirement,
    TaskEnvelope,
    TDDPlan,
)
from converge_orchestrator.prompts import builder_prompt

_TARGET_REQUIREMENT_STATEMENT = (
    "Builder context SHALL contain the validated Task Envelope, exact target requirement, "
    "applicable constraints, and deterministic test or verifier evidence relevant to the candidate."
)

_PROHIBITED_NARRATIVE_MARKERS: tuple[str, ...] = (
    "Scout snapshot",
    "repo_scout",
    "working-memory",
    "working memory",
    "last_review_findings",
    "Builder said",
    "Builder reported",
    "Builder output",
    "Raw builder",
    "raw builder",
    "builder's raw output",
    "prior role output",
)


def _requirements() -> list[Requirement]:
    return [
        Requirement(
            id="REQ-CONTEXT-002",
            statement=_TARGET_REQUIREMENT_STATEMENT,
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L270",
        ),
        Requirement(
            id="REQ-OTHER-001",
            statement="Unrelated requirement that Builder context must not receive.",
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L100",
        ),
        Requirement(
            id="REQ-CONTEXT-003",
            statement="Future context-handoff requirement pending.",
            source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L280",
        ),
    ]


def _task(*, tdd_mode: str = "not_applicable") -> TaskEnvelope:
    return TaskEnvelope(
        id="REQ-CONTEXT-002-0038",
        requirement_ids=["REQ-CONTEXT-002"],
        title="Pin builder context composition",
        objective="Lock the exact content of the builder context per REQ-CONTEXT-002.",
        constraints=[
            "Do not modify production code or public API",
            "Keep all changes inside tests/",
            "Do not weaken, delete, or skip existing tests",
            "Respect ARCH-CTRL-002 bounded-artifact prohibitions",
        ],
        allowed_paths=["tests/test_req_context_002_builder_context.py", "tests/**"],
        acceptance=[
            "New pin module passes under the configured deterministic pytest gate",
            "Existing pinned builder-prompt tests still pass",
        ],
        max_diff_lines=250,
        risk="low",
        risk_flags=[],
        change_kind="test_only",
        tdd=TDDPlan(
            mode=tdd_mode,  # type: ignore[arg-type]
            test_paths=["tests/test_req_context_002_builder_context.py"],
            test_gate=None,
            expected_failure_pattern="REQ-CONTEXT-002 expected RED marker",
            rationale="Pure test_only compliance pin; red-before-green does not apply.",
        ),
    )


def _red_evidence() -> dict:
    return {
        "red_test_sha256": {
            "tests/test_req_context_002_builder_context.py": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )
        },
        "test_gate": "pytest tests/test_req_context_002_builder_context.py",
        "expected_failure_pattern": "REQ-CONTEXT-002 expected RED marker",
    }


def test_req_context_002_builder_context_contains_validated_task_envelope() -> None:
    """The builder prompt carries the full validated Task Envelope."""
    task = _task()
    prompt = builder_prompt(task, _requirements())
    task_json = task.model_dump_json(indent=2)

    assert task.id in prompt
    assert task.title in prompt
    assert task.objective in prompt
    for constraint in task.constraints:
        assert constraint in prompt
    for path in task.allowed_paths:
        assert path in prompt
    for criterion in task.acceptance:
        assert criterion in prompt
    assert str(task.max_diff_lines) in prompt
    assert task.change_kind in prompt
    assert task.tdd.mode in prompt
    assert task_json in prompt


def test_req_context_002_builder_context_includes_exact_target_requirement_only() -> None:
    """Only the exact target requirement excerpt is present; non-target IDs are absent."""
    task = _task()
    requirements = _requirements()
    prompt = builder_prompt(task, requirements)

    target_header = "REQ-CONTEXT-002 | CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L270"
    assert target_header in prompt
    assert _TARGET_REQUIREMENT_STATEMENT in prompt

    for requirement in requirements:
        if requirement.id not in task.requirement_ids:
            assert requirement.id not in prompt
            assert requirement.statement not in prompt
            assert requirement.source not in prompt


def test_req_context_002_builder_context_includes_tdd_red_evidence_for_required_tdd() -> None:
    """For TDD-required candidates, the deterministic RED evidence artifact is included."""
    task = _task(tdd_mode="required")
    red_evidence = _red_evidence()
    prompt = builder_prompt(task, _requirements(), red_evidence)

    assert "TDD RED EVIDENCE" in prompt
    assert "The orchestrator has already verified a test-only RED phase" in prompt
    assert json.dumps(red_evidence, ensure_ascii=False, indent=2) in prompt
    assert red_evidence["expected_failure_pattern"] in prompt


def test_req_context_002_builder_context_omits_tdd_evidence_when_not_applicable() -> None:
    """For non-TDD candidates, no RED evidence or TDD narrative appears."""
    task = _task(tdd_mode="not_applicable")
    red_evidence = _red_evidence()
    prompt = builder_prompt(task, _requirements(), red_evidence)

    assert "TDD RED EVIDENCE" not in prompt
    assert "test-only RED phase" not in prompt
    assert json.dumps(red_evidence, ensure_ascii=False, indent=2) not in prompt


def test_req_context_002_builder_context_has_no_cross_role_narrative() -> None:
    """The builder prompt remains a bounded artifact with no cross-role narrative."""
    task = _task(tdd_mode="required")
    prompt = builder_prompt(task, _requirements(), _red_evidence())
    lowered = prompt.lower()
    assert not any(marker.lower() in lowered for marker in _PROHIBITED_NARRATIVE_MARKERS)
