"""Pin REQ-CONTEXT-004: explicit truncation declarations in working memory.

The deterministic working-memory projection produced by
``converge_orchestrator.context.build_working_memory`` is advisory continuity
context consumed by planner advisories and persisted as working-memory-*.json.
It already applies the bounds required by REQ-CONTEXT-004 (12 findings,
500-character reason, 32 requirement ids, 1000-character objective), but it does
not yet declare when those bounds truncate the original surface. This test
verifies that every bounded surface carries an explicit truncation declaration so
that blocking review findings and Task Envelope semantics are never silently
weakened.
"""

from __future__ import annotations

from typing import Any, cast

from converge_orchestrator.context import build_working_memory
from converge_orchestrator.models import WorkflowState

# Literal RED-lock marker. The new truncation declarations do not exist yet, so
# any attempt to read them from build_working_memory output fails with this
# exact marker until build_working_memory exposes the required keys.
_MARKER = "REQCTX004_TRUNCATION_DECLARATIONS_MISSING"


def _working_memory(state: dict[str, Any]) -> dict[str, Any]:
    """Return bounded working-memory projection, gating REQ-CONTEXT-004 keys.

    If the truncation declaration keys are absent from the projection, this
    helper raises AssertionError with the literal marker
    ``REQCTX004_TRUNCATION_DECLARATIONS_MISSING`` (deterministic RED lock)
    rather than allowing the caller to silently treat truncated content as
    complete.
    """
    memory = build_working_memory(cast(WorkflowState, state))

    if "last_review_findings_total" not in memory:
        raise AssertionError(_MARKER)
    if "last_review_findings_truncated" not in memory:
        raise AssertionError(_MARKER)

    for finding in memory.get("last_review_findings", []):
        if "reason_truncated" not in finding:
            raise AssertionError(_MARKER)

    last_task = memory.get("last_task")
    if last_task is not None:
        if "requirement_ids_truncated" not in last_task:
            raise AssertionError(_MARKER)
        if "objective_truncated" not in last_task:
            raise AssertionError(_MARKER)

    return memory


def test_review_findings_truncation_is_declared() -> None:
    # 14 blocking findings, each with a reason longer than the 500-character bound.
    long_reason = "x" * 600
    findings = [
        {
            "severity": "blocking",
            "requirement_id": f"REQ-{index:03d}",
            "file": "src/foo.py",
            "reason": long_reason,
        }
        for index in range(14)
    ]
    state = {
        "requirements": [],
        "compliance": {"entries": {}},
        "review_result": {"findings": findings},
    }

    memory = _working_memory(state)

    assert memory["last_review_findings_total"] == 14
    assert memory["last_review_findings_truncated"] is True
    assert len(memory["last_review_findings"]) == 12
    for finding in memory["last_review_findings"]:
        assert finding["reason_truncated"] is True
        assert len(finding["reason"]) <= 500


def test_last_task_truncation_is_declared() -> None:
    # A task envelope whose requirement_ids list exceeds the 32-id bound and
    # whose objective exceeds the 1000-character bound.
    state = {
        "requirements": [],
        "compliance": {"entries": {}},
        "task": {
            "id": "REQ-CONTEXT-004-0038",
            "title": "Declare bounded truncation explicitly in working memory",
            "requirement_ids": [f"REQ-{index:03d}" for index in range(35)],
            "objective": "y" * 1200,
        },
    }

    memory = _working_memory(state)

    assert memory["last_task"] is not None
    assert memory["last_task"]["requirement_ids_truncated"] is True
    assert len(memory["last_task"]["requirement_ids"]) == 32
    assert memory["last_task"]["objective_truncated"] is True
    assert len(memory["last_task"]["objective"]) <= 1000


def test_small_state_truncation_flags_are_false() -> None:
    # A small state that fits comfortably within every bound must still expose
    # the declaration keys, but all truncation flags must be false and totals
    # must match the original sizes exactly.
    short_findings = [
        {
            "severity": "blocking",
            "requirement_id": "REQ-001",
            "file": "src/foo.py",
            "reason": "short reason",
        },
        {
            "severity": "blocking",
            "requirement_id": "REQ-002",
            "file": "src/bar.py",
            "reason": "another short reason",
        },
    ]
    state = {
        "requirements": [],
        "compliance": {"entries": {}},
        "review_result": {"findings": short_findings},
        "task": {
            "id": "REQ-CONTEXT-004-0038",
            "title": "Small task",
            "requirement_ids": ["REQ-001", "REQ-002"],
            "objective": "short objective",
        },
    }

    memory = _working_memory(state)

    assert memory["last_review_findings_total"] == 2
    assert memory["last_review_findings_truncated"] is False
    assert len(memory["last_review_findings"]) == 2
    for finding in memory["last_review_findings"]:
        assert finding["reason_truncated"] is False

    assert memory["last_task"]["requirement_ids_truncated"] is False
    assert memory["last_task"]["objective_truncated"] is False
    assert len(memory["last_task"]["requirement_ids"]) == 2
    assert len(memory["last_task"]["objective"]) == len("short objective")
