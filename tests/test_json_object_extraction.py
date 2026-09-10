"""Regression tests for robust JSON-object extraction from LLM output.

V7 external acceptance failure (run ``cf92a4f6294f4a0393ce40e9c7bbe33f``): the planner emitted
narrative analysis that contained a Python set literal (``{"command", "stdout", ...}``) followed
by a complete, valid Task Envelope. The first-``{``-to-last-``}`` span parse failed on the set
literal, so the valid envelope was falsely rejected as a planner contract failure. Four
consecutive rejections exhausted the bounded planner correction budget and forced a
``planner_human`` interrupt during external acceptance.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from converge_orchestrator import opencode, workflow
from converge_orchestrator.acceptance_supervisor import (
    AcceptanceSupervisorError,
    _parse_review,
)
from converge_orchestrator.models import TaskEnvelope

ENVELOPE = json.dumps(
    {
        "id": "REQ-F92FFC55BA-0001",
        "requirement_ids": ["REQ-F92FFC55BA"],
        "title": "Add additive structured command simulation function",
        "objective": "Add simulate_command returning a structured dict.",
        "constraints": ["Do not modify existing public functions"],
        "allowed_paths": [
            "shared_tools/fake_terminal.py",
            "tests/test_shared_tools_fake_terminal.py",
        ],
        "acceptance": ["New test test_simulate_command_returns_structured_result passes"],
        "max_diff_lines": 40,
        "risk": "low",
        "risk_flags": [],
        "change_kind": "behavior",
        "tdd": {
            "mode": "required",
            "test_paths": ["tests/test_shared_tools_fake_terminal.py"],
            "test_gate": None,
            "expected_failure_pattern": "test_simulate_command_returns_structured_result",
            "rationale": "New observable function requires a failing test first.",
        },
    },
    indent=2,
)

# Faithful reproduction of the V7 planner output shape: narrative prose containing a
# brace group that is NOT JSON (Python set literal), followed by the real envelope.
NARRATIVE_WITH_SET_LITERAL = (
    "Let me analyze the repository.\n"
    "The compliance gap is the absence of an additive public function that returns a structured\n"
    "result: simulate_command(command: str) -> dict returning {\"command\", \"stdout\", "
    "\"stderr\", \"exit_code\"}, consistent with existing run_command output, plus a TDD test.\n"
    "Plan: add one additive public function.\n" + ENVELOPE
)

ENVELOPE_DICT = json.loads(ENVELOPE)

# V12 run ``4d75aa63837c44908c89c78c6856fa73`` (event line 73): the planner echoed the TDD
# schema fragment from its own prompt as a complete JSON object, then produced the final valid
# envelope in the same reply. The first-JSON heuristic grabbed the echo and falsely rejected
# the reply as a contract failure; the exhausted correction budget then forced a
# ``planner_failure_budget`` HITL during external acceptance.
TDD_SCHEMA_ECHO = json.dumps(
    {
        "mode": "required",
        "test_paths": ["tests/test_shared_tools_fake_terminal.py"],
        "test_gate": None,
        "expected_failure_pattern": "test_simulate_command_returns_structured_result",
        "rationale": "New observable function requires a failing test first.",
    }
)

# Tool-call payload decoy (V12): planner narration included its own todo bookkeeping as JSON.
TODOWRITE_TODO_ITEMS = json.dumps(
    {
        "todos": [
            {
                "content": "Inspect existing fake_terminal helpers",
                "status": "completed",
                "activeForm": "Inspecting existing fake_terminal helpers",
            },
            {
                "content": "Write the failing regression test",
                "status": "in_progress",
                "activeForm": "Writing the failing regression test",
            },
        ]
    }
)


def test_whole_text_object() -> None:
    payload = {"verdict": "pass", "findings": []}
    assert workflow._json_object(json.dumps(payload)) == payload
    assert opencode._json_object(json.dumps(payload)) == payload


def test_set_literal_decoy_before_valid_envelope() -> None:
    extracted = workflow._json_object(NARRATIVE_WITH_SET_LITERAL)
    assert extracted["id"] == "REQ-F92FFC55BA-0001"
    assert extracted["tdd"]["expected_failure_pattern"] == (
        "test_simulate_command_returns_structured_result"
    )
    # The reviewer-side copy must behave identically.
    assert opencode._json_object(NARRATIVE_WITH_SET_LITERAL)["id"] == "REQ-F92FFC55BA-0001"


def test_fenced_json_after_narrative() -> None:
    text = "Analysis follows.\n```json\n" + ENVELOPE + "\n```\nEnd of report."
    assert workflow._json_object(text)["id"] == "REQ-F92FFC55BA-0001"


def test_object_with_trailing_prose() -> None:
    text = json.dumps({"id": "T-1"}) + "\n\nI also verified the CI workflow."
    assert workflow._json_object(text) == {"id": "T-1"}


def test_nested_braces_in_strings_and_prose() -> None:
    payload = {"objective": 'render f"{value}" safely', "nested": {"a": 1}}
    text = "Note: template {value} braces in prose.\n" + json.dumps(payload)
    assert workflow._json_object(text) == payload


def test_no_braces_raises_agent_error() -> None:
    with pytest.raises(ValueError, match="Agent did not return a JSON object"):
        workflow._json_object("Plain narrative answer without any JSON payload.")


def test_no_braces_raises_reviewer_error() -> None:
    with pytest.raises(ValueError, match="reviewer did not return a JSON object"):
        opencode._json_object("Plain narrative answer without any JSON payload.")


def test_non_dict_json_raises() -> None:
    with pytest.raises(ValueError, match="Agent output must be a JSON object"):
        workflow._json_object('["not", "an", "object"]')
    with pytest.raises(ValueError, match="reviewer output must be a JSON object"):
        opencode._json_object('["not", "an", "object"]')


def test_final_audit_parse_with_decoy_braces() -> None:
    verdict = {"verdict": "pass", "findings": []}
    text = 'Brace decoy {"command", "stdout"} in prose.\n' + json.dumps(verdict)
    parsed = _parse_review("requirements", text)
    assert parsed.verdict == "pass"


def test_final_audit_parse_without_json_raises() -> None:
    with pytest.raises(AcceptanceSupervisorError, match="did not return JSON"):
        _parse_review("requirements", "Narrative audit answer without any JSON payload.")


# --- Task Envelope reply selection (V12 regression) -----------------------------


def test_whole_text_task_envelope_fast_path() -> None:
    envelope = workflow._task_envelope_output(ENVELOPE)
    assert envelope == TaskEnvelope.model_validate(ENVELOPE_DICT)


def test_schema_echo_before_valid_envelope_selects_the_final_object() -> None:
    envelope = workflow._task_envelope_output("\n\n".join([TDD_SCHEMA_ECHO, ENVELOPE]))
    assert envelope == TaskEnvelope.model_validate(ENVELOPE_DICT)


def test_empty_object_decoy_before_valid_envelope() -> None:
    envelope = workflow._task_envelope_output("\n\n".join(["{}", ENVELOPE]))
    assert envelope == TaskEnvelope.model_validate(ENVELOPE_DICT)


def test_todowrite_todo_item_decoys_before_valid_envelope() -> None:
    envelope = workflow._task_envelope_output("\n\n".join([TODOWRITE_TODO_ITEMS, ENVELOPE]))
    assert envelope.id == "REQ-F92FFC55BA-0001"


def test_multiple_decoys_with_narrative_select_the_final_valid_envelope() -> None:
    text = "\n".join(
        [
            "Reasoning narrative with a set literal {\"a\", \"b\"} in prose.",
            TDD_SCHEMA_ECHO,
            TODOWRITE_TODO_ITEMS,
            "{}",
            "Final answer:\n",
            ENVELOPE,
        ]
    )
    assert workflow._task_envelope_output(text) == TaskEnvelope.model_validate(ENVELOPE_DICT)


def test_two_valid_envelopes_select_the_final_one() -> None:
    earlier = dict(ENVELOPE_DICT, id="REQ-F92FFC55BA-0000", title="Earlier draft")
    envelope = workflow._task_envelope_output("\n\n".join([json.dumps(earlier), ENVELOPE]))
    assert envelope.id == "REQ-F92FFC55BA-0001"


def test_decoys_only_preserve_first_candidate_validation_error() -> None:
    text = "\n\n".join([TDD_SCHEMA_ECHO, "{}"])
    with pytest.raises(ValidationError, match="Field required"):
        workflow._task_envelope_output(text)


def test_no_json_raises_agent_error() -> None:
    with pytest.raises(ValueError, match="Agent did not return a JSON object"):
        workflow._task_envelope_output("Plain narrative answer without any JSON payload.")
