from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from converge_orchestrator.architecture import (
    load_baseline_architecture_cache,
    run_architecture_gate,
)
from converge_orchestrator.models import ProjectConfig
from converge_orchestrator.quality import run_quality_gates


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _config(
    tmp_path: Path,
    baseline: Path,
    *,
    rules: list[dict] | None = None,
) -> ProjectConfig:
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must preserve dependency boundaries.\n", encoding="utf-8")
    return ProjectConfig(
        repo_path=baseline,
        requirements_path=requirements,
        state_dir=tmp_path / ".converge",
        agents={},
        auto_discover_quality=False,
        require_spec_read_only=False,
        architecture={"python_import_rules": rules or []},
    )


def _boundary(**overrides) -> dict:  # type: ignore[no-untyped-def]
    rule = {
        "name": "domain-does-not-depend-on-infrastructure",
        "source_paths": ["src/app/domain"],
        "forbidden_imports": ["app.infrastructure"],
        "allowed_imports": [],
        "forbid_relative_imports": False,
        "required": True,
    }
    rule.update(overrides)
    return rule


def test_existing_architecture_debt_is_tolerated_but_new_violation_blocks(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/legacy.py", "import app.infrastructure.db\n")
    _write(candidate, "src/app/domain/legacy.py", "import app.infrastructure.db\n")
    _write(candidate, "src/app/domain/new.py", "from app.infrastructure.http import Client\n")
    config = _config(tmp_path, baseline, rules=[_boundary()])

    result = run_architecture_gate(config, candidate)

    assert result is not None
    assert result.ok is False
    details = json.loads(result.output)
    assert details["baseline_issue_count"] == 1
    assert details["candidate_issue_count"] == 2
    assert details["new_required_issue_count"] == 1
    assert details["new_issues"][0]["path"] == "src/app/domain/new.py"


def test_candidate_may_keep_or_remove_preexisting_violation(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    same_candidate = tmp_path / "same"
    repaired_candidate = tmp_path / "repaired"
    _write(baseline, "src/app/domain/model.py", "import app.infrastructure.db\n")
    _write(same_candidate, "src/app/domain/model.py", "import app.infrastructure.db\n")
    _write(repaired_candidate, "src/app/domain/model.py", "VALUE = 1\n")
    config = _config(tmp_path, baseline, rules=[_boundary()])

    unchanged = run_architecture_gate(config, same_candidate)
    repaired = run_architecture_gate(config, repaired_candidate)

    assert unchanged is not None and unchanged.ok is True
    assert repaired is not None and repaired.ok is True
    repaired_details = json.loads(repaired.output)
    assert repaired_details["resolved_issue_count"] == 1


def test_allowed_prefix_does_not_hide_other_forbidden_imports(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/model.py", "VALUE = 1\n")
    _write(
        candidate,
        "src/app/domain/model.py",
        "from app.infrastructure.types import DTO\nimport app.infrastructure.db\n",
    )
    config = _config(
        tmp_path,
        baseline,
        rules=[_boundary(allowed_imports=["app.infrastructure.types"])],
    )

    result = run_architecture_gate(config, candidate)

    assert result is not None and result.ok is False
    details = json.loads(result.output)
    assert [issue["module"] for issue in details["new_issues"]] == ["app.infrastructure.db"]


def test_relative_imports_are_checked_only_when_explicitly_forbidden(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/model.py", "VALUE = 1\n")
    _write(candidate, "src/app/domain/model.py", "from .local import VALUE\n")
    config = _config(
        tmp_path,
        baseline,
        rules=[_boundary(forbid_relative_imports=True)],
    )

    result = run_architecture_gate(config, candidate)

    assert result is not None and result.ok is False
    details = json.loads(result.output)
    assert details["new_issues"][0]["kind"] == "relative_import"
    assert details["new_issues"][0]["module"] == ".local"


def test_new_parse_error_under_boundary_fails_closed(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/model.py", "VALUE = 1\n")
    _write(candidate, "src/app/domain/model.py", "def broken(:\n    pass\n")
    config = _config(tmp_path, baseline, rules=[_boundary()])

    result = run_architecture_gate(config, candidate)

    assert result is not None and result.ok is False
    details = json.loads(result.output)
    assert details["new_issues"][0]["kind"] == "parse_error"


def test_baseline_cache_is_bound_to_commit_and_policy_hash(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/model.py", "VALUE = 1\n")
    _write(candidate, "src/app/domain/model.py", "VALUE = 1\n")
    config = _config(tmp_path, baseline, rules=[_boundary()])

    with patch("converge_orchestrator.architecture.current_head", return_value="base-sha"):
        first = run_architecture_gate(config, candidate)
        second = run_architecture_gate(config, candidate)

    assert first is not None and json.loads(first.output)["baseline_cache_hit"] is False
    assert second is not None and json.loads(second.output)["baseline_cache_hit"] is True
    assert load_baseline_architecture_cache(config, base_commit="base-sha") is not None

    changed = _config(
        tmp_path,
        baseline,
        rules=[_boundary(forbidden_imports=["app.adapters"])],
    )
    assert load_baseline_architecture_cache(changed, base_commit="base-sha") is None


def test_architecture_policy_is_part_of_regular_quality_results(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write(baseline, "src/app/domain/model.py", "VALUE = 1\n")
    _write(candidate, "src/app/domain/model.py", "import app.infrastructure.db\n")
    config = _config(tmp_path, baseline, rules=[_boundary()])

    results = run_quality_gates(config, candidate)

    assert [result.name for result in results] == ["architecture_imports"]
    assert results[0].ok is False


def test_boundary_configuration_rejects_ambiguous_or_escaping_paths(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()

    with pytest.raises(ValidationError, match="stay inside the repository"):
        _config(tmp_path, baseline, rules=[_boundary(source_paths=["../domain"])])

    with pytest.raises(ValidationError, match="literal POSIX paths"):
        _config(tmp_path, baseline, rules=[_boundary(source_paths=["src/**/domain"])])

    config = _config(
        tmp_path,
        baseline,
        rules=[_boundary(source_paths=["./src/app/domain/"])],
    )
    assert config.architecture.python_import_rules[0].source_paths == ["src/app/domain"]
