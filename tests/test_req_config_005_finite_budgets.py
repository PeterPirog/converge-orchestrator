"""REQ-CONFIG-005: project configuration expresses finite, non-negative budget envelopes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from converge_orchestrator.models import ProjectConfig

_BUDGET_FIELDS = (
    "max_repair_attempts",
    "max_replans",
    "max_iterations",
    "max_diff_lines_hard",
)


def _base_kwargs(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    requirements = tmp_path / "architecture.md"
    requirements.write_text("ARCH-001 System must remain bounded.\n", encoding="utf-8")
    return {
        "repo_path": repo,
        "requirements_path": requirements,
        "agents": {},
    }


def test_req_config_005_finite_budget_envelope_rejects_out_of_range_values(
    tmp_path: Path,
) -> None:
    """Negative repair/replan/iteration/diff budgets must fail closed, naming the field."""
    for field in _BUDGET_FIELDS:
        kwargs = _base_kwargs(tmp_path)
        kwargs[field] = -1
        with pytest.raises(ValidationError, match=field):
            ProjectConfig(**kwargs)


def test_req_config_005_budget_defaults_and_in_bounds_values_remain_valid(
    tmp_path: Path,
) -> None:
    """Documented defaults are preserved and the non-negative envelope keeps valid configs valid."""
    cfg = ProjectConfig(**_base_kwargs(tmp_path))
    assert cfg.max_repair_attempts == 3
    assert cfg.max_replans == 2
    assert cfg.max_iterations == 50
    assert cfg.max_diff_lines_hard == 1000

    floor = dict.fromkeys(_BUDGET_FIELDS, 0)
    ceil = dict.fromkeys(_BUDGET_FIELDS, 1)
    assert ProjectConfig(**_base_kwargs(tmp_path), **floor).max_diff_lines_hard == 0
    assert ProjectConfig(**_base_kwargs(tmp_path), **ceil).max_repair_attempts == 1
