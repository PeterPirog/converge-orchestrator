"""V30 final-audit parser regression tests.

V30 (run ``2bee1eb883dc4bc0af02f4b92a2da31b``) converged 12/12 mandatory requirements
with exactly one predeclared approved risk HITL, yet the release gate failed closed
because the final independent audit's security lane returned output that the supervisor's
first-JSON-object parser resolved to ``{}``, masking a later valid verdict. The workflow
review lanes already select the LAST structurally valid ``ReviewResult`` (PR #98
hardening); these tests prove the final audit now uses the identical authoritative
semantics and that failures persist bounded forensic evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from converge_orchestrator.acceptance_supervisor import (
    AcceptanceSupervisorError,
    SupervisorFailureRecord,
    SupervisorProgress,
    _parse_review,
    _write_failure_record,
)
from converge_orchestrator.models import ProjectConfig, ReviewResult
from converge_orchestrator.opencode import _review_result_output

_ALL_AUDIT_ROLES = (
    "requirements",
    "architecture",
    "compatibility",
    "security",
)


def _pass() -> str:
    return json.dumps({"verdict": "pass", "findings": []})


def _reject(reason: str = "unverified mandatory requirement") -> str:
    return json.dumps(
        {
            "verdict": "reject",
            "findings": [{"severity": "major", "reason": reason, "required_fix": None}],
        }
    )


# ---------------------------------------------------------------------------
# TEST A / B — unrelated or empty early JSON must not mask a later valid verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", _ALL_AUDIT_ROLES)
def test_unrelated_json_object_before_valid_verdict_selects_the_verdict(role: str) -> None:
    text = json.dumps({"id": "T-1", "metadata": {"note": "unrelated"}}) + "\n\n" + _pass()
    assert _parse_review(role, text).verdict == "pass"


@pytest.mark.parametrize("role", _ALL_AUDIT_ROLES)
def test_empty_object_decoy_before_valid_verdict_selects_the_verdict(role: str) -> None:
    text = "Preliminary check returned {} for the sample input.\n\n" + _pass()
    assert _parse_review(role, text).verdict == "pass"


# ---------------------------------------------------------------------------
# TEST C — a valid semantic REJECT remains REJECT
# ---------------------------------------------------------------------------


def test_valid_reject_is_preserved_even_after_decoys() -> None:
    text = json.dumps({"note": "decoy"}) + "\n\n" + _reject()
    assert _parse_review("security", text).verdict == "reject"
    parsed = _parse_review("security", _reject())
    assert parsed.findings[0].severity == "major"


# ---------------------------------------------------------------------------
# TEST D — unrelated JSON only fails closed
# ---------------------------------------------------------------------------


def test_unrelated_json_only_fails_closed() -> None:
    text = json.dumps({"id": "T-1"}) + "\n\nUnverified audit narrative without a verdict."
    with pytest.raises(AcceptanceSupervisorError, match="invalid review JSON") as excinfo:
        _parse_review("security", text)
    assert excinfo.value.failure_kind == "final_audit_invalid_json"
    assert "output tail:" in str(excinfo.value)
    assert '"id": "T-1"' in str(excinfo.value)


def test_empty_object_only_fails_closed() -> None:
    with pytest.raises(AcceptanceSupervisorError, match="invalid review JSON") as excinfo:
        _parse_review("compatibility", "{}")
    assert excinfo.value.failure_kind == "final_audit_invalid_json"


def test_narrative_without_json_fails_closed() -> None:
    with pytest.raises(AcceptanceSupervisorError, match="did not return JSON") as excinfo:
        _parse_review("requirements", "Narrative audit answer without any JSON payload.")
    assert excinfo.value.failure_kind == "final_audit_invalid_json"


# ---------------------------------------------------------------------------
# TEST E — last structurally valid candidate wins (workflow semantics)
# ---------------------------------------------------------------------------


def test_multiple_valid_reviews_select_the_last_one() -> None:
    text = "\n\n".join([_pass(), _reject(), _pass()])
    assert _parse_review("compatibility", text).verdict == "pass"
    text = "\n\n".join([_pass(), _reject()])
    assert _parse_review("compatibility", text).verdict == "reject"


def test_invalid_candidate_after_valid_one_selects_the_valid_one() -> None:
    text = "\n\n".join([_pass(), json.dumps({"findings": []}), "trailing narrative"])
    assert _parse_review("security", text).verdict == "pass"


# ---------------------------------------------------------------------------
# TEST F — pure valid ReviewResult JSON keeps the fast path
# ---------------------------------------------------------------------------


def test_pure_valid_review_result_fast_path_unchanged() -> None:
    parsed = _parse_review("requirements", _pass())
    assert parsed == ReviewResult.model_validate(json.loads(_pass()))
    assert _parse_review("architecture", _pass()).verdict == "pass"


# ---------------------------------------------------------------------------
# TEST G / H — bounded forensic evidence persisted in the failure record
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        state_dir=tmp_path / ".converge",
        worktree_dir=tmp_path / ".converge" / "worktrees",
        agents={},
        require_spec_read_only=False,
    )


def test_parsing_failure_persists_bounded_forensic_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = (
        "Unparseable narrative without any valid verdict. " * 40
        + json.dumps({"id": "T-1", "note": "schema-invalid candidate"})
    )
    with pytest.raises(AcceptanceSupervisorError, match="invalid review JSON") as excinfo:
        _parse_review("security", output)
    error = excinfo.value

    output_path = tmp_path / "acceptance-supervisor.json"
    _write_failure_record(
        output_path=output_path,
        config=config,
        project_id="external-acceptance-v31",
        run_id="run-1",
        exc=error,
        progress=SupervisorProgress(run_id="run-1"),
        expected_risk_flag="forbidden_public_api_change",
    )

    record = SupervisorFailureRecord.model_validate_json(
        output_path.read_text(encoding="utf-8")
    )
    evidence_copy = (
        config.state_dir / "evidence" / "run-1" / "external-acceptance-failure.json"
    )
    assert evidence_copy.is_file()
    assert json.loads(evidence_copy.read_text(encoding="utf-8")) == json.loads(
        output_path.read_text(encoding="utf-8")
    )
    assert record.failure_kind == "final_audit_invalid_json"
    assert record.run_id == "run-1"
    assert record.detail.startswith("security final audit returned invalid review JSON")
    assert "output tail:" in record.detail
    assert output[-2000:] in record.detail
    assert output[:2000] not in record.detail


def test_forensic_tail_is_deterministically_bounded(tmp_path: Path) -> None:
    output = "Narrative. " * 400 + json.dumps({"note": "y" * 3000})
    with pytest.raises(AcceptanceSupervisorError, match="invalid review JSON") as excinfo:
        _parse_review("requirements", output)
    detail = str(excinfo.value)
    assert "output tail: " + output[-2000:] in detail
    assert "output tail: " + output[-2001:] not in detail


# ---------------------------------------------------------------------------
# TEST J + PART 6 — no special casing and cross-parser consistency
# ---------------------------------------------------------------------------


_REPRESENTATIVE_OUTPUTS = (
    _pass(),
    _reject(),
    "{}\n\n" + _pass(),
    json.dumps({"id": "T-1"}) + "\n\n" + _reject(),
    "\n\n".join([_pass(), _reject(), _pass()]),
    "Analysis narrative.\n```json\n" + _pass() + "\n```\nMore narrative.",
)


@pytest.mark.parametrize("role", _ALL_AUDIT_ROLES)
@pytest.mark.parametrize("text", _REPRESENTATIVE_OUTPUTS)
def test_final_audit_matches_workflow_review_semantics(role: str, text: str) -> None:
    workflow_parsed = _review_result_output(text)
    audit_parsed = _parse_review(role, text)
    assert audit_parsed == workflow_parsed