from __future__ import annotations

import json
from typing import Any, cast

from converge_orchestrator.config import evaluate_mandatory_convergence_blocking
from converge_orchestrator.context import build_working_memory
from converge_orchestrator.models import WorkflowState

# Literal RED-lock marker: the bounded working-memory projection of recommended
# requirement records does not exist yet, so every attempt to read recommended
# record visibility from build_working_memory output fails with this exact marker
# until converge_orchestrator.context.build_working_memory exposes
# ``recommended_requirement_ids``.
_MARKER = "SOT013_RECOMMENDED_VISIBILITY_MISSING"


def _working_memory(state: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded working-memory projection for a run-state ``state``.

    REQ-SOT-013: recommended requirement records SHALL remain visible without
    becoming release blockers. ``converge_orchestrator.context.build_working_memory``
    is the pure, offline projection that enforces that contract over a plain
    run-state mapping holding ``requirements`` (the stable-ordered list of
    requirement records, each a mapping with a non-empty ``id`` and optional
    ``severity``) and ``compliance`` (a snapshot mapping of record id to a
    compliance-entry mapping carrying the record's status). The recommended
    visibility projection is not present yet, so this helper raises AssertionError
    with the literal marker ``SOT013_RECOMMENDED_VISIBILITY_MISSING``
    (deterministic RED lock) instead of silently treating non-PASS recommended
    records as invisible continuity context.
    """
    memory = build_working_memory(cast(WorkflowState, state))
    if "recommended_requirement_ids" not in memory:
        raise AssertionError(_MARKER)
    return memory


def test_recommended_non_pass_records_are_visible_and_do_not_block() -> None:
    # Recommended records whose status is not PASS must remain visible in the
    # working memory (in deterministic sorted order, not record order), while
    # never becoming release blockers: they stay absent from the unresolved
    # mandatory list and the convergence guard reports no clause for them.
    state = {
        "requirements": [
            {"id": "REQ-M1", "severity": "mandatory"},
            {"id": "REQ-R2", "severity": "recommended"},
            {"id": "REQ-R1", "severity": "recommended"},
        ],
        "compliance": {
            "entries": {
                "REQ-M1": {"requirement_id": "REQ-M1", "status": "pass", "evidence": []},
                "REQ-R1": {"requirement_id": "REQ-R1", "status": "fail", "evidence": []},
                "REQ-R2": {"requirement_id": "REQ-R2", "status": "unverified", "evidence": []},
            }
        },
    }

    memory = _working_memory(state)

    assert memory["recommended_requirement_ids"] == ["REQ-R1", "REQ-R2"]
    assert memory["unresolved_mandatory_requirement_ids"] == []
    assert evaluate_mandatory_convergence_blocking(state) == ()


def test_recommended_ids_are_deterministically_sorted_and_capped_at_64() -> None:
    # With more than 64 non-PASS recommended records the projection stays bounded
    # to the first 64 ids in deterministic sorted order (not stable record order).
    ids = [f"REC-{index:02d}" for index in range(70)]
    state = {
        "requirements": [{"id": item, "severity": "recommended"} for item in reversed(ids)],
        "compliance": {
            "entries": {
                item: {"requirement_id": item, "status": "fail", "evidence": []} for item in ids
            }
        },
    }

    memory = _working_memory(state)

    assert memory["recommended_requirement_ids"] == ids[:64]
    assert len(memory["recommended_requirement_ids"]) == 64
    assert "REC-69" not in memory["recommended_requirement_ids"]


def test_recommended_projection_contains_ids_only() -> None:
    # The projection is ids only: requirement statements and evidence bodies never
    # leak into the bounded working-memory artifact.
    state = {
        "requirements": [
            {
                "id": "REQ-R1",
                "severity": "recommended",
                "statement": "Sensitive advisory statement R1",
                "source": "architecture.md:1",
            }
        ],
        "compliance": {
            "entries": {
                "REQ-R1": {
                    "requirement_id": "REQ-R1",
                    "status": "fail",
                    "evidence": ["Sensitive evidence body R1"],
                }
            }
        },
    }

    memory = _working_memory(state)
    serialized = json.dumps(memory)

    assert memory["recommended_requirement_ids"] == ["REQ-R1"]
    assert "Sensitive advisory statement" not in serialized
    assert "Sensitive evidence body" not in serialized


def test_no_recommended_records_yields_empty_visibility_list() -> None:
    # Without any recommended record the projection is present but empty, while the
    # mandatory blocking surface is untouched.
    state = {
        "requirements": [{"id": "REQ-M1", "severity": "mandatory"}],
        "compliance": {
            "entries": {"REQ-M1": {"requirement_id": "REQ-M1", "status": "fail", "evidence": []}}
        },
    }

    memory = _working_memory(state)

    assert memory["recommended_requirement_ids"] == []
    assert memory["unresolved_mandatory_requirement_ids"] == ["REQ-M1"]
    assert evaluate_mandatory_convergence_blocking(state) == (
        "mandatory_requirement_blocks_convergence:REQ-M1",
    )
