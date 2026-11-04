"""Pin REQ-AGENT-001: deterministic core agent role set and independent review lanes.

REQ-AGENT-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L245
[7. Deterministic architecture and agent roles]): The core role set SHALL include repository
reconnaissance, bounded planning, candidate implementation, correctness review, architecture
review, and security review.

These deterministic offline assertions lock the built-in role catalog to the six REQ-AGENT-001
core roles and verify that the three independent review lanes are supported by the review
role registry and required by the acceptance supervisor. No network, model, provider, git
command, subprocess call, or filesystem mutation is performed.
"""

from __future__ import annotations

from converge_orchestrator.acceptance_supervisor import _REQUIRED_REVIEW_ROLES
from converge_orchestrator.models import _REVIEW_AGENT_ROLES
from converge_orchestrator.opencode_config import _ROLE_DEFINITIONS

# REQ-AGENT-001 canonical core role set.
_REQ_AGENT_001_CORE_ROLES = {
    "scout",
    "planner",
    "builder",
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
}

# REQ-AGENT-001 independent review lanes.
_REQ_AGENT_001_REVIEW_LANES = {
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
}


def test_req_agent_001_core_role_set_is_built_in_with_non_empty_prompts() -> None:
    """The six REQ-AGENT-001 core roles are present and carry non-empty prompts."""
    missing = _REQ_AGENT_001_CORE_ROLES - set(_ROLE_DEFINITIONS)
    assert not missing, (
        "REQ-AGENT-001 core role set missing from _ROLE_DEFINITIONS: "
        f"{sorted(missing)}"
    )

    for role in sorted(_REQ_AGENT_001_CORE_ROLES):
        definition = _ROLE_DEFINITIONS[role]
        prompt = definition.get("prompt", "")
        assert isinstance(prompt, str) and prompt.strip(), (
            f"REQ-AGENT-001 core role {role!r} must have a non-empty prompt"
        )


def test_req_agent_001_three_independent_review_lanes_are_supported_and_required() -> None:
    """All three independent review lanes are supported and exactly required."""
    unsupported = _REQ_AGENT_001_REVIEW_LANES - _REVIEW_AGENT_ROLES
    assert not unsupported, (
        "REQ-AGENT-001 independent review lanes missing from _REVIEW_AGENT_ROLES: "
        f"{sorted(unsupported)}"
    )

    assert _REQUIRED_REVIEW_ROLES == _REQ_AGENT_001_REVIEW_LANES, (
        "REQ-AGENT-001 requires exactly the three independent review lanes in "
        "_REQUIRED_REVIEW_ROLES; got "
        f"{sorted(_REQUIRED_REVIEW_ROLES)}"
    )
