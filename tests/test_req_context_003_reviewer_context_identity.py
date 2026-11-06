"""REQ-CONTEXT-003: reviewer context contains exact candidate content identity."""

from __future__ import annotations

import hashlib

from converge_orchestrator.models import Requirement, TaskEnvelope
from converge_orchestrator.prompts import reviewer_prompt


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        id="REQ-CONTEXT-003-0001",
        requirement_ids=["REQ-CONTEXT-003"],
        title="Render candidate content identity",
        objective="Reviewer context must include the exact SHA-256 of the rendered diff.",
    )


def _requirement() -> Requirement:
    return Requirement(
        id="REQ-CONTEXT-003",
        statement="Reviewer context SHALL contain the exact candidate content identity.",
        source="CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L271",
    )


def test_reviewer_prompt_renders_candidate_content_identity() -> None:
    task = _task()
    requirement = _requirement()
    diff_text = "diff --git a/src/example.py b/src/example.py\n+added line\n-removed line\n"

    prompt = reviewer_prompt(task, diff_text, [requirement])

    assert "CANDIDATE CONTENT IDENTITY" in prompt, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        "CANDIDATE CONTENT IDENTITY section missing"
    )

    expected_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    assert expected_sha256 in prompt, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        f"expected SHA-256 {expected_sha256} not found"
    )

    identity_position = prompt.index("CANDIDATE CONTENT IDENTITY")
    diff_position = prompt.index("DIFF:")
    assert identity_position < diff_position, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        "CANDIDATE CONTENT IDENTITY section must appear before the DIFF section"
    )


def test_reviewer_prompt_identity_tracks_exact_diff_content() -> None:
    task = _task()
    requirement = _requirement()
    diff_a = "diff a\n"
    diff_b = "diff b\n"

    prompt_a = reviewer_prompt(task, diff_a, [requirement])
    prompt_b = reviewer_prompt(task, diff_b, [requirement])

    sha_a = hashlib.sha256(diff_a.encode("utf-8")).hexdigest()
    sha_b = hashlib.sha256(diff_b.encode("utf-8")).hexdigest()

    assert sha_a in prompt_a, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        "identity for diff_a missing"
    )
    assert sha_b in prompt_b, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        "identity for diff_b missing"
    )
    assert sha_a not in prompt_b, (
        "REQ-CONTEXT-003 reviewer context lacks exact candidate content identity: "
        "identity must change when diff content changes"
    )
