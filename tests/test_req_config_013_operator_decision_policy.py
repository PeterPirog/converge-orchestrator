"""REQ-CONFIG-013: operator-decision autonomy policy is declared and fail-closed.

REQ-CONFIG-013 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L211
[6. Project configuration and secret sources]): project configuration SHALL
define whether operator decisions are permitted during a run, and an
unattended autonomous policy SHALL NOT acquire a hidden dependency on
interactive approval, confirmation, or manual resume.

These offline, deterministic tests pin that contract at the public
``converge_orchestrator.config.load_config`` entry point:

* a configuration without ``workflow.operator_decisions`` remains valid and
  exposes an ``operator_decisions`` policy resolving to ``permitted=True``,
  ``unattended_autonomous=False`` and no ``interactive_dependencies``;
* an explicit attended ``workflow.operator_decisions`` policy loads through
  ``load_config`` with its declared ``permitted`` value and
  ``interactive_dependencies`` intact;
* an unattended-autonomous policy with no declared interactive dependencies
  loads successfully;
* an unattended-autonomous policy declaring an interactive dependency on
  approval, confirmation, or manual resume fails closed with a validation
  error naming ``workflow.operator_decisions`` and the offending dependency.

No network, model, provider, subprocess, or git access occurs outside pytest
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import load_config
from converge_orchestrator.models import ProjectConfig

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by the failure
# assertions below until the operator_decisions policy surface exists on ProjectConfig.
POLICY_MISSING = "REQ-CONFIG-013-OPERATOR-DECISION-POLICY-MISSING"

_POLICY_FIELDS = ("permitted", "unattended_autonomous", "interactive_dependencies")
_INTERACTIVE_DEPENDENCIES = (
    "interactive_approval",
    "interactive_confirmation",
    "manual_resume",
)


def _base_payload() -> dict[str, Any]:
    """One valid converge.yaml mapping with a single planner agent."""
    return {
        "version": 1,
        "project": {
            "name": "operator-decision-policy",
            "repo_path": "./repository",
            "requirements_path": "./requirements.md",
            "state_dir": "./state",
            "worktree_dir": "./state/worktrees",
            "require_spec_read_only": False,
        },
        "models": {"profiles": {"planner": {"model": "anthropic/example"}}},
        "agents": {
            "planner": {
                "agent": "converge-planner",
                "model_profile": "planner",
            },
        },
    }


def _write_config(base: Path, payload: dict[str, Any]) -> Path:
    """Materialize the target repository, Source of Truth, and converge.yaml under base."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(
        "# Requirements\n\n"
        "- REQ-CONFIG-013 unattended runs must not depend on operator decisions.\n",
        encoding="utf-8",
    )
    config_path = base / "converge.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return config_path


def _policy(cfg: ProjectConfig) -> Any:
    """Return the operator_decisions policy, failing closed when the surface is missing."""
    assert hasattr(cfg, "operator_decisions"), (
        f"{POLICY_MISSING}: ProjectConfig must expose an operator_decisions policy"
    )
    return cfg.operator_decisions


def test_absent_operator_decisions_resolves_permitted_and_attended(tmp_path: Path) -> None:
    """A config without workflow.operator_decisions stays valid with the default policy."""
    cfg = load_config(_write_config(tmp_path / "absent", _base_payload()))

    policy = _policy(cfg)
    for field in _POLICY_FIELDS:
        assert hasattr(policy, field), (
            f"{POLICY_MISSING}: operator_decisions must expose the {field!r} field"
        )
    assert (
        policy.permitted,
        policy.unattended_autonomous,
        policy.interactive_dependencies,
    ) == (True, False, []), (
        f"{POLICY_MISSING}: an absent workflow.operator_decisions section must resolve to "
        "permitted=True, unattended_autonomous=False and no interactive dependencies"
    )


@pytest.mark.parametrize("permitted", [True, False], ids=["permitted-true", "permitted-false"])
def test_explicit_attended_policy_loads_declared_values_unchanged(
    tmp_path: Path, permitted: bool
) -> None:
    """An attended workflow.operator_decisions section loads with its declared values."""
    payload = _base_payload()
    section: dict[str, Any] = {
        "permitted": permitted,
        "unattended_autonomous": False,
        "interactive_dependencies": list(_INTERACTIVE_DEPENDENCIES) if permitted else [],
    }
    payload["workflow"] = {"operator_decisions": section}
    cfg = load_config(_write_config(tmp_path / f"attended-{str(permitted).lower()}", payload))

    policy = _policy(cfg)
    assert (
        policy.permitted,
        policy.unattended_autonomous,
        policy.interactive_dependencies,
    ) == (
        permitted,
        False,
        section["interactive_dependencies"],
    ), (
        f"{POLICY_MISSING}: a declared attended operator_decisions policy must load unchanged"
    )


def test_unattended_autonomous_without_interactive_dependencies_loads(tmp_path: Path) -> None:
    """An unattended-autonomous policy without interactive dependencies loads successfully."""
    payload = _base_payload()
    section = {
        "permitted": True,
        "unattended_autonomous": True,
        "interactive_dependencies": [],
    }
    payload["workflow"] = {"operator_decisions": section}
    cfg = load_config(_write_config(tmp_path / "unattended-clean", payload))

    policy = _policy(cfg)
    assert (
        policy.permitted,
        policy.unattended_autonomous,
        policy.interactive_dependencies,
    ) == (True, True, []), (
        f"{POLICY_MISSING}: an unattended-autonomous policy without interactive "
        "dependencies must load successfully"
    )


@pytest.mark.parametrize(
    "dependency",
    _INTERACTIVE_DEPENDENCIES,
    ids=["interactive-approval", "interactive-confirmation", "manual-resume"],
)
def test_unattended_autonomous_with_interactive_dependency_fails_closed(
    tmp_path: Path, dependency: str
) -> None:
    """An unattended policy depending on operator interaction fails closed and names the gap."""
    payload = _base_payload()
    payload["workflow"] = {
        "operator_decisions": {
            "permitted": True,
            "unattended_autonomous": True,
            "interactive_dependencies": [dependency],
        }
    }
    config_path = _write_config(tmp_path / f"fail-closed-{dependency}", payload)

    raised: Exception | None = None
    try:
        load_config(config_path)
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        raised = exc

    assert raised is not None, (
        f"{POLICY_MISSING}: an unattended_autonomous operator_decisions policy declaring "
        f"{dependency!r} must fail closed"
    )
    message = str(raised)
    assert "workflow.operator_decisions" in message, (
        f"{POLICY_MISSING}: the validation error must name workflow.operator_decisions"
    )
    assert dependency in message, (
        f"{POLICY_MISSING}: the validation error must name the offending "
        f"dependency {dependency!r}"
    )
