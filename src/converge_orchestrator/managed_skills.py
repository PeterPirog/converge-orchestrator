from __future__ import annotations

from pathlib import Path

from .models import ProjectConfig

_MANAGED_SKILLS: dict[str, str] = {
    "requirements-compliance": """---
name: requirements-compliance
description: Trace work to immutable architecture requirements and detect drift.
---
# Requirements compliance
Treat the supplied architecture requirements as authoritative and read-only. Cite requirement IDs
and source anchors in plans and reviews. Never resolve ambiguity by weakening the requirements.
Prefer measurable evidence from deterministic tests, dependency checks, static analysis and build
output. Never regress a mandatory requirement that already passes.
""",
    "repo-scout": """---
name: repo-scout
description: Build a compact evidence-backed repository map without choosing work.
---
# Repository scout
Inspect only what is needed to map the current base commit. Report relevant modules, tests,
dependency boundaries, risky surfaces and uncertainties. Separate observations from inference.
Do not select the next task, propose broad modernization, edit files or carry hidden conversational
history forward. Keep the handoff compact enough to be advisory input to the Planner.
""",
    "bounded-planning": """---
name: bounded-planning
description: Turn one deterministic target requirement into the smallest verifiable task envelope.
---
# Bounded planning
Plan exactly one bounded convergence step for the target requirement chosen by the deterministic
scheduler. Prefer the smallest change with executable acceptance evidence. Define allowed paths,
acceptance criteria, change kind and TDD obligations precisely. Do not write implementation code,
change the target requirement, expand scope to unrelated cleanup or use another agent as hidden
memory.
""",
    "test-driven-change": """---
name: test-driven-change
description: Implement bounded changes with focused regression tests and minimal diffs.
---
# Test-driven change
Inspect first. For behavior changes preserve the orchestrator-verified RED artifact and implement
the smallest production change that makes the same deterministic gate GREEN. Run focused tests and
then the configured quality gates. Do not weaken, delete, skip or xfail valid tests merely to make
the build green. Stay inside the Task Envelope.
""",
    "correctness-review": """---
name: correctness-review
description: Independently review observable behavior, edge cases, tests and compatibility.
---
# Correctness review
Review the actual diff and surrounding code, not the Builder narrative. Look for regressions, edge
cases, incorrect assumptions, weak tests and hidden compatibility changes. Findings must be
concrete and evidence-backed. Do not edit the worktree or silently reinterpret acceptance criteria.
""",
    "architecture-review": """---
name: architecture-review
description: Independently review architecture boundaries and immutable requirement compliance.
---
# Architecture review
Review the actual diff against requirement IDs and source anchors. Check dependency direction,
boundaries, public contracts, coupling, scope and needless complexity. Reject changes that satisfy a
local task by weakening the intended architecture. Do not edit files and do not defer to the Builder
narrative.
""",
    "security-review": """---
name: security-review
description: Independently review security properties and trust-boundary changes in a diff.
---
# Security review
Inspect the actual diff and the minimum surrounding code needed to assess authentication,
authorization, secret handling, command/path construction, injection, insecure defaults, dependency
risk and trust boundaries. Prefer evidence over speculation, but treat uncertain high-impact
security changes conservatively. Never expose credentials in findings and never edit the worktree.
""",
}

_DEFAULT_ROLE_SKILLS: dict[str, tuple[str, ...]] = {
    "scout": ("repo-scout", "requirements-compliance"),
    "planner": ("bounded-planning", "requirements-compliance"),
    "builder": ("test-driven-change", "requirements-compliance"),
    "reviewer": (
        "correctness-review",
        "architecture-review",
        "security-review",
        "requirements-compliance",
    ),
    "correctness_reviewer": ("correctness-review", "requirements-compliance"),
    "architecture_reviewer": ("architecture-review", "requirements-compliance"),
    "security_reviewer": ("security-review", "requirements-compliance"),
}


def effective_role_skills(role: str) -> tuple[str, ...]:
    """Return Converge's fixed trusted Skill allowlist for one runtime role."""
    return _DEFAULT_ROLE_SKILLS.get(role, ())


def materialize_managed_skills(config: ProjectConfig) -> Path:
    """Create trusted runtime Skills outside the target repository and return config directory."""
    root = config.state_dir / "opencode-runtime"
    skills_root = root / "skills"
    for name, body in _MANAGED_SKILLS.items():
        target = skills_root / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.rstrip() + "\n", encoding="utf-8")
    return root
