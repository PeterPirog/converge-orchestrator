"""REQ-CONFIG-006: configuration validation completes before any model call or repository mutation.

REQ-CONFIG-006 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L204
[6. Project configuration and secret sources]): configuration validation SHALL
complete before the first model call or target-repository mutation.

These offline, deterministic regression tests lock that ordering invariant on the two
effectful entry points a run reaches first. Both nodes make ``load_config`` their very
first statement, so a converge.yaml that the configuration schema rejects must raise
inside ``load_config`` — before the node can reach any mutation or model call:

* ``workflow.bootstrap`` — the first target-repository mutation in the graph is
  ``ensure_clean``; an invalid config must raise before ``ensure_clean`` runs.
* ``graph.scout`` — the first base-branch refresh and model call in the graph are
  ``update_base`` and ``OpenCodeAdapter.invoke``; an invalid config must raise before
  either runs.

No network, model, provider, subprocess, or git access occurs outside pytest tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from converge_orchestrator import workflow
from converge_orchestrator.config import load_config
from converge_orchestrator.graph import scout

_REQUIREMENTS_TEXT = "REQ-001 The orchestrator converges the repository.\n"


def _write_invalid_config(project: Path) -> Path:
    """Write a converge.yaml whose pre-validation passes but the schema rejects.

    ``max_repair_attempts`` is bounded ``ge=0`` in :class:`ProjectConfig`; the negative
    value forces configuration validation to fail closed inside ``load_config``.
    """

    (project / "repository").mkdir(parents=True, exist_ok=True)
    (project / "requirements.md").write_text(_REQUIREMENTS_TEXT, encoding="utf-8")
    data = {
        "version": 1,
        "repo_path": "./repository",
        "requirements_path": "./requirements.md",
        "agents": {},
        "max_repair_attempts": -1,
    }
    config_path = project / "converge.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return config_path


def _state(config_path: Path) -> dict:
    return {
        "config_path": str(config_path),
        "run_id": "run-1",
        "thread_id": "thread-1",
        "requirements_hash": "sha",
        "requirements": [
            {
                "id": "REQ-001",
                "statement": "The orchestrator converges the repository.",
                "source": "requirements.md:1",
            }
        ],
        "iteration": 0,
        "repair_attempts": 0,
        "replan_attempts": 0,
        "risk_flags": [],
        "approved_risk_flags": [],
        "status": "spec_ok",
    }


def test_invalid_config_raises_before_bootstrap_mutates_repository(
    tmp_path: Path,
) -> None:
    """An invalid converge.yaml raises in load_config before workflow.ensure_clean runs.

    ``ensure_clean`` is the first target-repository mutation the bootstrap node reaches;
    if configuration validation did not complete before it, the node would have already
    touched the target repository. Asserting the budget-field error pins the failure to
    configuration validation rather than a downstream effect.
    """

    config_path = _write_invalid_config(tmp_path)
    state = _state(config_path)

    # The rejection is configuration validation itself, not a downstream side effect.
    with pytest.raises(ValueError, match="max_repair_attempts"):
        load_config(config_path)

    with patch.object(workflow, "ensure_clean") as mock_ensure_clean:
        with pytest.raises(ValueError, match="max_repair_attempts"):
            workflow.bootstrap(state)

    mock_ensure_clean.assert_not_called()


def test_invalid_config_raises_before_scout_model_call_and_base_refresh(
    tmp_path: Path,
) -> None:
    """An invalid converge.yaml raises in load_config before scout's first model call.

    ``graph.scout`` refreshes the base branch (``update_base``) and then invokes a model
    (``OpenCodeAdapter.invoke``) after ``load_config``; an invalid config must raise
    before either effect. Pinning the budget-field error keeps the failure on the
    configuration-validation path rather than on a downstream mutation.
    """

    config_path = _write_invalid_config(tmp_path)
    state = _state(config_path)

    with pytest.raises(ValueError, match="max_repair_attempts"):
        load_config(config_path)

    with (
        patch("converge_orchestrator.graph.update_base") as mock_update_base,
        patch("converge_orchestrator.graph.OpenCodeAdapter.invoke") as mock_invoke,
    ):
        with pytest.raises(ValueError, match="max_repair_attempts"):
            scout(state)

    mock_update_base.assert_not_called()
    mock_invoke.assert_not_called()
