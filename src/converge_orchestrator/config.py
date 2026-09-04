from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from .ci_flakes import flaky_ci_policy_from_mapping
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


def _load_mapping(source: Path) -> dict[str, Any]:
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("converge.yaml must contain a YAML mapping at the document root")
    flaky_ci_policy_from_mapping(data)
    return _resolve_relative_paths(data, source.parent)


def _validated_config(data: dict[str, Any]) -> ProjectConfig:
    cfg = ProjectConfig.model_validate(data)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def load_config(path: str | Path) -> ProjectConfig:
    source = Path(path).expanduser().resolve()
    return _validated_config(_load_mapping(source))


def materialize_run_config_snapshot(
    source_path: str | Path,
    run_id: str,
) -> tuple[ProjectConfig, Path, str]:
    """Freeze one validated project configuration for the lifetime of a durable run."""
    source = Path(source_path).expanduser().resolve()
    data = _load_mapping(source)
    cfg = _validated_config(data)
    target = cfg.state_dir / "run-configs" / f"{run_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(f"Run configuration snapshot already exists: {target}")

    content = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return cfg, target.resolve(), digest


def load_run_config_snapshot(path: str | Path, expected_sha256: str) -> ProjectConfig:
    """Load a pinned run configuration only when its durable content hash still matches."""
    source = Path(path).expanduser().resolve()
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            "Pinned run configuration changed; refusing to continue durable execution "
            f"(expected {expected_sha256}, got {actual})"
        )
    return load_config(source)
