"""REQ-CONFIG-002: canonical converge.yaml relative paths anchor on the YAML file's
directory, never the process working directory.

These regression tests lock CWD-independence for every path-bearing key (repo_path,
requirements_path, state_dir, worktree_dir, and opencode.generated_config_path). They
are deterministic and offline: no network, model, or subprocess behavior is involved.
"""

from __future__ import annotations

from pathlib import Path

from converge_orchestrator.config import load_config


def _write_relative_config(project: Path) -> Path:
    """Write a canonical converge.yaml whose path-bearing keys are all relative."""
    (project / "repository").mkdir(parents=True, exist_ok=True)
    (project / "architecture.md").write_text(
        "System must remain stable.\n", encoding="utf-8"
    )
    config_path = project / "converge.yaml"
    config_path.write_text(
        """
version: 1
project:
  repo_path: ./repository
  requirements_path: ./architecture.md
  state_dir: ./.state
  worktree_dir: ./.worktrees
  require_spec_read_only: false
opencode:
  generated_config_path: ./.runtime/opencode.json
agents:
  planner:
    agent: converge-planner
    model: openai/example
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _path_bearing_values(cfg):
    return [
        cfg.repo_path,
        cfg.requirements_path,
        cfg.state_dir,
        cfg.worktree_dir,
        cfg.opencode_generated_config_path,
    ]


def _assert_resolved_against_yaml_dir(cfg, project: Path, cwd: Path) -> None:
    """Every path-bearing key must anchor on the YAML directory, not the CWD."""
    project_root = project.resolve()
    cwd_root = cwd.resolve()
    for value in _path_bearing_values(cfg):
        assert value.is_absolute()
        assert value.is_relative_to(project_root), f"{value} is not under the YAML directory"
        assert not value.is_relative_to(cwd_root), f"{value} leaked into the CWD {cwd_root}"


def test_relative_paths_resolve_against_yaml_dir_not_process_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = _write_relative_config(project)

    # Repoint the process working directory at an unrelated location.
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    cfg = load_config(config_path)

    assert cfg.repo_path == (project / "repository").resolve()
    assert cfg.requirements_path == (project / "architecture.md").resolve()
    assert cfg.state_dir == (project / ".state").resolve()
    assert cfg.worktree_dir == (project / ".worktrees").resolve()
    assert cfg.opencode_generated_config_path == (project / ".runtime/opencode.json").resolve()
    _assert_resolved_against_yaml_dir(cfg, project, unrelated_cwd)


def test_same_config_loads_identically_from_two_working_dirs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = _write_relative_config(project)

    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = load_config(config_path)
    monkeypatch.chdir(second_cwd)
    second = load_config(config_path)

    # Identical load from two unrelated CWDs: resolution is a pure function of the file.
    assert first == second
    _assert_resolved_against_yaml_dir(first, project, first_cwd)
    _assert_resolved_against_yaml_dir(second, project, second_cwd)
