"""REQ-CONFIG-011: manual-authoring waits and abandonment are independent of provider retries.

REQ-CONFIG-011 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L209
[6. Project configuration and secret sources]): configuration that enables manual
authoring SHALL define operator-wait and candidate-abandonment policy independently
from provider retry policy so human editing time is not interpreted as provider failure.

These offline, deterministic tests pin that contract at the public
``converge_orchestrator.config.load_config`` entry point:

* a configuration without ``workflow.manual_authoring`` remains valid and exposes a
  disabled ``manual_authoring`` policy carrying the ``enabled``,
  ``operator_wait_seconds`` and ``candidate_abandonment_seconds`` fields;
* an enabled ``workflow.manual_authoring`` section with explicit
  ``operator_wait_seconds`` and ``candidate_abandonment_seconds`` loads through
  ``load_config`` with its declared values intact;
* the same enabled policy loads unchanged when only the agent ``provider_retries``
  value differs, so human editing time is never derived from provider retry policy;
* enabling manual authoring without both explicit durations fails closed with a
  validation error naming the ``manual_authoring`` policy and the missing field(s).

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
# assertions below until the manual_authoring policy surface exists on ProjectConfig.
POLICY_MISSING = "REQ-CONFIG-011-MANUAL-AUTHORING-POLICY-MISSING"

_POLICY_FIELDS = ("enabled", "operator_wait_seconds", "candidate_abandonment_seconds")
_OPERATOR_WAIT = 900
_ABANDONMENT = 3600
_BOTH_MISSING = ("candidate_abandonment_seconds", "operator_wait_seconds")
_ABANDONMENT_MISSING = ("candidate_abandonment_seconds",)
_WAIT_MISSING = ("operator_wait_seconds",)


def _base_payload(provider_retries: int) -> dict[str, Any]:
    """One valid converge.yaml mapping with a single planner agent and explicit retries."""
    return {
        "version": 1,
        "project": {
            "name": "manual-authoring-policy",
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
                "provider_retries": provider_retries,
            },
        },
    }


def _enabled_payload(provider_retries: int) -> dict[str, Any]:
    """The base payload with an enabled manual-authoring policy and explicit durations."""
    payload = _base_payload(provider_retries)
    payload["workflow"] = {
        "manual_authoring": {
            "enabled": True,
            "operator_wait_seconds": _OPERATOR_WAIT,
            "candidate_abandonment_seconds": _ABANDONMENT,
        }
    }
    return payload


def _write_config(base: Path, payload: dict[str, Any]) -> Path:
    """Materialize the target repository, Source of Truth, and converge.yaml under base."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(
        "# Requirements\n\n- REQ-CONFIG-011 manual authoring waits are not provider retries.\n",
        encoding="utf-8",
    )
    config_path = base / "converge.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return config_path


def _policy(cfg: ProjectConfig) -> Any:
    """Return the manual_authoring policy, failing closed when the surface is missing."""
    assert hasattr(cfg, "manual_authoring"), (
        f"{POLICY_MISSING}: ProjectConfig must expose a manual_authoring policy"
    )
    return cfg.manual_authoring


def test_manual_authoring_is_disabled_by_default_when_section_absent(tmp_path: Path) -> None:
    """A config without workflow.manual_authoring stays valid and the policy is off."""
    cfg = load_config(_write_config(tmp_path / "disabled", _base_payload(0)))

    policy = _policy(cfg)
    assert policy.enabled is False, POLICY_MISSING
    for field in _POLICY_FIELDS:
        assert hasattr(policy, field), (
            f"{POLICY_MISSING}: manual_authoring must expose the {field!r} field"
        )


def test_manual_authoring_loads_with_explicit_durations(tmp_path: Path) -> None:
    """An enabled workflow.manual_authoring section loads with its declared durations."""
    cfg = load_config(_write_config(tmp_path / "enabled", _enabled_payload(1)))

    policy = _policy(cfg)
    assert (
        policy.enabled,
        policy.operator_wait_seconds,
        policy.candidate_abandonment_seconds,
    ) == (True, _OPERATOR_WAIT, _ABANDONMENT), (
        f"{POLICY_MISSING}: declared manual_authoring durations must load unchanged"
    )


def test_manual_authoring_stays_independent_of_provider_retries(tmp_path: Path) -> None:
    """Only the agent provider_retries value differs; the manual-authoring policy is unchanged."""
    cfg_a = load_config(_write_config(tmp_path / "retries-0", _enabled_payload(0)))
    cfg_b = load_config(_write_config(tmp_path / "retries-3", _enabled_payload(3)))

    assert cfg_a.agents["planner"].provider_retries == 0
    assert cfg_b.agents["planner"].provider_retries == 3

    policy_a = _policy(cfg_a)
    policy_b = _policy(cfg_b)
    assert (
        policy_a.enabled,
        policy_a.operator_wait_seconds,
        policy_a.candidate_abandonment_seconds,
    ) == (
        policy_b.enabled,
        policy_b.operator_wait_seconds,
        policy_b.candidate_abandonment_seconds,
    ) == (True, _OPERATOR_WAIT, _ABANDONMENT), (
        f"{POLICY_MISSING}: manual authoring waits must not be derived from provider_retries"
    )


@pytest.mark.parametrize(
    ("section", "missing_fields"),
    [
        ({"enabled": True}, _BOTH_MISSING),
        ({"enabled": True, "operator_wait_seconds": _OPERATOR_WAIT}, _ABANDONMENT_MISSING),
        ({"enabled": True, "candidate_abandonment_seconds": _ABANDONMENT}, _WAIT_MISSING),
    ],
    ids=["both-durations-missing", "candidate-abandonment-missing", "operator-wait-missing"],
)
def test_enabled_manual_authoring_without_durations_fails_closed(
    tmp_path: Path,
    section: dict[str, Any],
    missing_fields: tuple[str, ...],
) -> None:
    """Enabling manual authoring without explicit durations fails closed and names the gap."""
    payload = _base_payload(0)
    payload["workflow"] = {"manual_authoring": section}
    config_path = _write_config(tmp_path / "fail-closed", payload)

    raised: Exception | None = None
    try:
        load_config(config_path)
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        raised = exc

    assert raised is not None, (
        f"{POLICY_MISSING}: enabling manual authoring without explicit "
        f"{', '.join(missing_fields)} must fail closed"
    )
    message = str(raised)
    assert "manual_authoring" in message, (
        f"{POLICY_MISSING}: the validation error must name the manual_authoring policy"
    )
    for field in missing_fields:
        assert field in message, (
            f"{POLICY_MISSING}: the validation error must name the missing field {field!r}"
        )
