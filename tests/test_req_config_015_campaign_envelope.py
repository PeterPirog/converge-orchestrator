"""REQ-CONFIG-015: unattended autonomous continuation is bounded by a pinned campaign envelope.

REQ-CONFIG-015 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L213
[6. Project configuration and secret sources]): unattended autonomous continuation
across multiple runs SHALL use a pinned finite convergence-campaign envelope that
bounds at least successor-run count and cumulative model-attempt, conservative-token,
and machine-execution authority independently from the finite envelope of each
individual run.

These offline, deterministic tests pin that contract at the public
``converge_orchestrator.config.load_config`` entry point:

* a configuration without ``workflow.campaign_envelope`` remains valid and exposes a
  ``campaign_envelope`` resolving to a disabled envelope with no bounds;
* an enabled ``workflow.campaign_envelope`` with ``pinned: true`` and finite
  non-negative ``max_successor_runs``, ``max_cumulative_model_attempts``,
  ``max_cumulative_estimated_tokens`` and
  ``max_cumulative_machine_execution_seconds`` loads through ``load_config`` with its
  declared values intact;
* enabling the envelope without ``pinned: true`` or while omitting any required bound
  fails closed with a validation error naming ``workflow.campaign_envelope`` and each
  missing field;
* negative or non-integer bounds are rejected with a validation error naming the
  offending field;
* the campaign envelope is pinned with the configuration: it appears in a materialized
  run configuration snapshot and reloads unchanged through
  ``load_run_config_snapshot``;
* campaign limits are independent of ``workflow.run_budget``: per-run budget field
  names do not satisfy campaign bounds, changing ``run_budget`` does not change the
  campaign limits, and both surfaces load with distinct declared values.

No network, model, provider, subprocess, or git access occurs outside pytest
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from converge_orchestrator.config import (
    load_config,
    load_run_config_snapshot,
    materialize_run_config_snapshot,
)
from converge_orchestrator.models import ProjectConfig

# TDD RED evidence marker (tdd.expected_failure_pattern): carried by the failure
# assertions below until the campaign_envelope surface exists on ProjectConfig.
ENVELOPE_MISSING = "REQ-CONFIG-015-CAMPAIGN-ENVELOPE-POLICY-MISSING"

_BOUND_FIELDS = (
    "max_successor_runs",
    "max_cumulative_model_attempts",
    "max_cumulative_estimated_tokens",
    "max_cumulative_machine_execution_seconds",
)
_MAX_SUCCESSOR_RUNS = 2
_MAX_CUMULATIVE_MODEL_ATTEMPTS = 10
_MAX_CUMULATIVE_TOKENS = 10_000
_MAX_CUMULATIVE_SECONDS = 100


def _base_payload() -> dict[str, Any]:
    """One valid converge.yaml mapping with a single planner agent."""
    return {
        "version": 1,
        "project": {
            "name": "campaign-envelope",
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


def _valid_envelope() -> dict[str, Any]:
    """A pinned, fully-bounded campaign envelope with distinct declared values."""
    return {
        "pinned": True,
        "max_successor_runs": _MAX_SUCCESSOR_RUNS,
        "max_cumulative_model_attempts": _MAX_CUMULATIVE_MODEL_ATTEMPTS,
        "max_cumulative_estimated_tokens": _MAX_CUMULATIVE_TOKENS,
        "max_cumulative_machine_execution_seconds": _MAX_CUMULATIVE_SECONDS,
    }


def _write_config(base: Path, payload: dict[str, Any]) -> Path:
    """Materialize the target repository, Source of Truth, and converge.yaml under base."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "repository").mkdir(parents=True, exist_ok=True)
    (base / "requirements.md").write_text(
        "# Requirements\n\n"
        "- REQ-CONFIG-015 campaign continuation uses a pinned finite envelope.\n",
        encoding="utf-8",
    )
    config_path = base / "converge.yaml"
    config_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return config_path


def _envelope(cfg: ProjectConfig) -> Any:
    """Return the campaign_envelope, failing closed when the surface is missing."""
    assert hasattr(cfg, "campaign_envelope"), (
        f"{ENVELOPE_MISSING}: ProjectConfig must expose a campaign_envelope"
    )
    return cfg.campaign_envelope


def _load_expect_failure(config_path: Path) -> Exception | None:
    """Load a config that must fail closed, returning the raised exception or None."""
    try:
        load_config(config_path)
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        return exc
    return None


def test_absent_campaign_envelope_resolves_disabled_without_bounds(tmp_path: Path) -> None:
    """A config without workflow.campaign_envelope stays valid and the envelope is off."""
    cfg = load_config(_write_config(tmp_path / "absent", _base_payload()))

    env = _envelope(cfg)
    assert env.pinned is False, (
        f"{ENVELOPE_MISSING}: an absent workflow.campaign_envelope must resolve to a "
        "disabled envelope"
    )
    for field in _BOUND_FIELDS:
        assert getattr(env, field) is None, (
            f"{ENVELOPE_MISSING}: a disabled campaign envelope must carry no declared "
            f"bound on {field!r}"
        )


def test_enabled_campaign_envelope_loads_declared_values(tmp_path: Path) -> None:
    """An enabled, pinned, fully-bounded campaign envelope loads with its declared values."""
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": _valid_envelope()}
    cfg = load_config(_write_config(tmp_path / "enabled", payload))

    env = _envelope(cfg)
    assert (
        env.pinned,
        env.max_successor_runs,
        env.max_cumulative_model_attempts,
        env.max_cumulative_estimated_tokens,
        env.max_cumulative_machine_execution_seconds,
    ) == (
        True,
        _MAX_SUCCESSOR_RUNS,
        _MAX_CUMULATIVE_MODEL_ATTEMPTS,
        _MAX_CUMULATIVE_TOKENS,
        _MAX_CUMULATIVE_SECONDS,
    ), (
        f"{ENVELOPE_MISSING}: declared campaign_envelope values must load unchanged"
    )


def test_enabled_campaign_envelope_without_pinned_fails_closed(tmp_path: Path) -> None:
    """Enabling the envelope without pinned=true fails closed and names the gap."""
    section = _valid_envelope()
    section["pinned"] = False
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": section}
    config_path = _write_config(tmp_path / "fail-closed-no-pinned", payload)

    raised = _load_expect_failure(config_path)
    assert raised is not None, (
        f"{ENVELOPE_MISSING}: enabling workflow.campaign_envelope without pinned=true "
        "must fail closed"
    )
    message = str(raised)
    assert "workflow.campaign_envelope" in message, (
        f"{ENVELOPE_MISSING}: the validation error must name workflow.campaign_envelope"
    )
    assert "pinned" in message, (
        f"{ENVELOPE_MISSING}: the validation error must name the missing pinned field"
    )


@pytest.mark.parametrize("missing", _BOUND_FIELDS, ids=list(_BOUND_FIELDS))
def test_enabled_campaign_envelope_missing_bound_fails_closed(
    tmp_path: Path, missing: str
) -> None:
    """Enabling the envelope while omitting a required bound fails closed and names it."""
    section = _valid_envelope()
    del section[missing]
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": section}
    config_path = _write_config(tmp_path / f"fail-closed-missing-{missing}", payload)

    raised = _load_expect_failure(config_path)
    assert raised is not None, (
        f"{ENVELOPE_MISSING}: enabling workflow.campaign_envelope without {missing!r} "
        "must fail closed"
    )
    message = str(raised)
    assert "workflow.campaign_envelope" in message, (
        f"{ENVELOPE_MISSING}: the validation error must name workflow.campaign_envelope"
    )
    assert missing in message, (
        f"{ENVELOPE_MISSING}: the validation error must name the missing field {missing!r}"
    )


@pytest.mark.parametrize("field", _BOUND_FIELDS, ids=list(_BOUND_FIELDS))
def test_negative_campaign_bound_rejected(tmp_path: Path, field: str) -> None:
    """A negative campaign bound is rejected with a validation error naming the field."""
    section = _valid_envelope()
    section[field] = -1
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": section}
    config_path = _write_config(tmp_path / f"fail-closed-negative-{field}", payload)

    raised = _load_expect_failure(config_path)
    assert raised is not None, f"{ENVELOPE_MISSING}: a negative {field} must be rejected"
    assert field in str(raised), (
        f"{ENVELOPE_MISSING}: the validation error must name the offending "
        f"field {field!r}"
    )


@pytest.mark.parametrize("field", _BOUND_FIELDS, ids=list(_BOUND_FIELDS))
def test_non_integer_campaign_bound_rejected(tmp_path: Path, field: str) -> None:
    """A non-integer campaign bound is rejected with a validation error naming the field."""
    section = _valid_envelope()
    section[field] = 1.5
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": section}
    config_path = _write_config(tmp_path / f"fail-closed-non-integer-{field}", payload)

    raised = _load_expect_failure(config_path)
    assert raised is not None, (
        f"{ENVELOPE_MISSING}: a non-integer {field} must be rejected"
    )
    assert field in str(raised), (
        f"{ENVELOPE_MISSING}: the validation error must name the offending "
        f"field {field!r}"
    )


def test_campaign_envelope_pinned_in_run_snapshot(tmp_path: Path) -> None:
    """The campaign envelope is pinned with the configuration in a run snapshot."""
    payload = _base_payload()
    payload["workflow"] = {"campaign_envelope": _valid_envelope()}
    source = _write_config(tmp_path / "snapshot", payload)

    _, snapshot, digest = materialize_run_config_snapshot(source, "run-1")
    reloaded = load_run_config_snapshot(snapshot, digest)

    env = _envelope(reloaded)
    assert (
        env.pinned,
        env.max_successor_runs,
        env.max_cumulative_model_attempts,
        env.max_cumulative_estimated_tokens,
        env.max_cumulative_machine_execution_seconds,
    ) == (
        True,
        _MAX_SUCCESSOR_RUNS,
        _MAX_CUMULATIVE_MODEL_ATTEMPTS,
        _MAX_CUMULATIVE_TOKENS,
        _MAX_CUMULATIVE_SECONDS,
    ), (
        f"{ENVELOPE_MISSING}: the campaign envelope must reload unchanged from the "
        "pinned run configuration snapshot"
    )


def test_campaign_envelope_independent_of_run_budget(tmp_path: Path) -> None:
    """Campaign bounds stay independent of the per-run workflow.run_budget envelope."""
    envelope = _valid_envelope()
    payload_a = _base_payload()
    payload_a["workflow"] = {
        "run_budget": {"max_model_attempts": 5},
        "campaign_envelope": envelope,
    }
    payload_b = _base_payload()
    payload_b["workflow"] = {
        "run_budget": {"max_model_attempts": 999},
        "campaign_envelope": envelope,
    }
    cfg_a = load_config(_write_config(tmp_path / "independent-a", payload_a))
    cfg_b = load_config(_write_config(tmp_path / "independent-b", payload_b))

    assert cfg_a.run_budget.max_model_attempts == 5
    assert cfg_b.run_budget.max_model_attempts == 999

    env_a = _envelope(cfg_a)
    env_b = _envelope(cfg_b)
    assert (
        env_a.max_successor_runs,
        env_a.max_cumulative_model_attempts,
        env_a.max_cumulative_estimated_tokens,
        env_a.max_cumulative_machine_execution_seconds,
    ) == (
        env_b.max_successor_runs,
        env_b.max_cumulative_model_attempts,
        env_b.max_cumulative_estimated_tokens,
        env_b.max_cumulative_machine_execution_seconds,
    ) == (
        _MAX_SUCCESSOR_RUNS,
        _MAX_CUMULATIVE_MODEL_ATTEMPTS,
        _MAX_CUMULATIVE_TOKENS,
        _MAX_CUMULATIVE_SECONDS,
    ), (
        f"{ENVELOPE_MISSING}: campaign bounds must not be derived from "
        "workflow.run_budget"
    )
    assert env_a.max_cumulative_model_attempts != cfg_a.run_budget.max_model_attempts, (
        f"{ENVELOPE_MISSING}: campaign model-attempt authority must be declared "
        "independently of run_budget.max_model_attempts"
    )
