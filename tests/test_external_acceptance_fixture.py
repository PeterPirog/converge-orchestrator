"""Regression tests for the external acceptance fixture.

The first external acceptance run compiled 13 mandatory records because the descriptive
preamble ended with the sentence "The acceptance run must not edit this document.", which
the normative-requirements parser compiled as ``REQ-02A987FD5B``. Under the stable source
order targeting policy that preamble-derived record outranked every ``ACCEPT-001`` record.
The preamble is descriptive prose, so it must never compile into an actionable requirement
that precedes ``ACCEPT-001``.
"""

from pathlib import Path

from converge_orchestrator.models import ComplianceSnapshot, ProjectConfig
from converge_orchestrator.spec import compile_contract
from converge_orchestrator.targeting import choose_target_requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPO_ROOT
    / "examples"
    / "external-acceptance"
    / "converge-orchestrator-test-repo"
    / "requirements.md"
)

PREAMBLE_HEADING = "Converge external acceptance requirements"
ACCEPT_001_HEADING = "ACCEPT-001 — Structured command simulation"
ACCEPT_002_HEADING = "ACCEPT-002 — Deterministic secret redaction helper"
ACCEPT_003_HEADING = (
    "ACCEPT-003 — Deliberate compatibility exception for release-gate HITL proof"
)

EXPECTED_IDS = [
    "REQ-F92FFC55BA",
    "REQ-879DB2129D",
    "REQ-0C50BE10F3",
    "REQ-413A5B74FD",
    "REQ-85C52948B7",
    "REQ-A59E470230",
    "REQ-CF0D222BF0",
    "REQ-0320AB815A",
    "REQ-5C3F7AB352",
    "REQ-280A8C4BB0",
    "REQ-AB50309F6F",
    "REQ-B7BFA18E79",
]


def _fixture_requirements():  # type: ignore[no-untyped-def]
    return compile_contract(FIXTURE).requirements


def test_acceptance_fixture_compiles_exactly_the_12_actionable_ids() -> None:
    requirements = _fixture_requirements()
    ids = [requirement.id for requirement in requirements]

    assert "REQ-02A987FD5B" not in ids
    assert len(requirements) == 12
    assert ids == EXPECTED_IDS


def test_acceptance_fixture_has_no_preamble_requirement_before_accept_001() -> None:
    sources = [requirement.source for requirement in _fixture_requirements()]
    first_accept_001 = next(
        index for index, source in enumerate(sources) if ACCEPT_001_HEADING in source
    )
    preamble_indexes = [
        index for index, source in enumerate(sources) if PREAMBLE_HEADING in source
    ]

    assert all(index > first_accept_001 for index in preamble_indexes)


def test_acceptance_fixture_first_requirement_is_accept_001() -> None:
    first = _fixture_requirements()[0]

    assert ACCEPT_001_HEADING in first.source
    assert first.id == "REQ-F92FFC55BA"


def test_acceptance_fixture_targets_first_accept_001_requirement() -> None:
    requirements = _fixture_requirements()
    config = ProjectConfig(
        repo_path=REPO_ROOT,
        requirements_path=FIXTURE,
        state_dir=REPO_ROOT / ".converge",
        worktree_dir=REPO_ROOT / ".converge" / "worktrees",
        requirement_verifiers={},
        agents={},
        require_spec_read_only=False,
    )

    target = choose_target_requirement(requirements, ComplianceSnapshot(), config)

    assert target is not None
    assert target.id == requirements[0].id
    assert ACCEPT_001_HEADING in target.source


def test_acceptance_fixture_keeps_accept_003_breaking_api_scenario() -> None:
    accept_003 = [
        requirement
        for requirement in _fixture_requirements()
        if ACCEPT_003_HEADING in requirement.source
    ]

    assert [requirement.id for requirement in accept_003] == [
        "REQ-280A8C4BB0",
        "REQ-AB50309F6F",
        "REQ-B7BFA18E79",
    ]
    assert any("format_output" in requirement.statement for requirement in accept_003)


def test_acceptance_fixture_requirements_trace_to_accept_sections() -> None:
    sources = [requirement.source for requirement in _fixture_requirements()]

    assert any(ACCEPT_001_HEADING in source for source in sources)
    assert any(ACCEPT_002_HEADING in source for source in sources)
    assert any(ACCEPT_003_HEADING in source for source in sources)
    assert all(source.startswith("requirements.md:L") for source in sources)