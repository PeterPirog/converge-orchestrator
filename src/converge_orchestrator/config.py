from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import ProjectConfig

_PATH_KEYS = (
    "repo_path",
    "requirements_path",
    "state_dir",
    "worktree_dir",
)


def _resolve_path_value(value: Any, base_dir: Path) -> Any:
    if value is None or isinstance(value, Path):
        return value
    if not isinstance(value, str):
        return value
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return str(candidate.resolve())


def _resolve_relative_paths(data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    resolved = dict(data)
    project = resolved.get("project")
    if isinstance(project, dict):
        project = dict(project)
        for key in _PATH_KEYS:
            if key in project:
                project[key] = _resolve_path_value(project[key], base_dir)
        resolved["project"] = project

    for key in _PATH_KEYS:
        if key in resolved:
            resolved[key] = _resolve_path_value(resolved[key], base_dir)

    opencode = resolved.get("opencode")
    if isinstance(opencode, dict):
        opencode = dict(opencode)
        if "generated_config_path" in opencode:
            opencode["generated_config_path"] = _resolve_path_value(
                opencode["generated_config_path"],
                base_dir,
            )
        resolved["opencode"] = opencode
    if "opencode_generated_config_path" in resolved:
        resolved["opencode_generated_config_path"] = _resolve_path_value(
            resolved["opencode_generated_config_path"],
            base_dir,
        )
    return resolved


def load_config(path: str | Path) -> ProjectConfig:
    source = Path(path).expanduser().resolve()
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("converge.yaml must contain a YAML mapping at the document root")
    data = _resolve_relative_paths(data, source.parent)
    cfg = ProjectConfig.model_validate(data)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_dir.mkdir(parents=True, exist_ok=True)
    return cfg
