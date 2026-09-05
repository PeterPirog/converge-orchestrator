from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from .ci_flakes import flaky_ci_policy_from_mapping
from .models import ModelProfile, ProjectConfig

_PATH_KEYS = (
    "repo_path",
    "requirements_path",
    "state_dir",
    "worktree_dir",
)
_MODEL_MODES = {"cloud", "local"}
_RUN_CONFIG_DIR = "run-configs"
_RUN_CONFIG_PATTERN = re.compile(r"^.+-sha256-([0-9a-f]{64})\.yaml$")


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


def _select_model_profile_set(data: dict[str, Any]) -> dict[str, Any]:
    """Select one validated cloud/local profile set before runtime config validation."""
    resolved = dict(data)
    models = resolved.get("models")
    if not isinstance(models, dict):
        return resolved

    mode = models.get("mode")
    profile_sets = models.get("profile_sets")
    if mode is None and profile_sets is None:
        return resolved

    if mode not in _MODEL_MODES:
        raise ValueError("models.mode must be one of: cloud, local")
    if "profiles" in models:
        raise ValueError("models.profiles cannot be combined with models.profile_sets")
    if not isinstance(profile_sets, dict) or not profile_sets:
        raise ValueError(
            "models.profile_sets must be a non-empty mapping when models.mode is set"
        )

    unknown_modes = sorted(set(profile_sets) - _MODEL_MODES)
    if unknown_modes:
        raise ValueError(
            f"models.profile_sets contains unsupported modes: {unknown_modes}; "
            "allowed: ['cloud', 'local']"
        )

    for set_name, profiles in profile_sets.items():
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError(f"models.profile_sets.{set_name} must be a non-empty mapping")
        for profile_name, profile in profiles.items():
            try:
                ModelProfile.model_validate(profile)
            except Exception as exc:
                raise ValueError(
                    f"invalid model profile models.profile_sets.{set_name}.{profile_name}: {exc}"
                ) from exc

    selected = profile_sets.get(mode)
    if selected is None:
        raise ValueError(f"models.profile_sets does not define selected mode {mode!r}")

    normalized_models = dict(models)
    normalized_models.pop("mode", None)
    normalized_models.pop("profile_sets", None)
    normalized_models["profiles"] = dict(selected)
    resolved["models"] = normalized_models
    return resolved


def _snapshot_digest_from_path(source: Path) -> str | None:
    if source.parent.name != _RUN_CONFIG_DIR:
        return None
    match = _RUN_CONFIG_PATTERN.fullmatch(source.name)
    if match is None:
        raise RuntimeError(f"Malformed pinned run configuration path: {source}")
    return match.group(1)


def _read_source(source: Path) -> str:
    payload = source.read_bytes()
    expected = _snapshot_digest_from_path(source)
    if expected is not None:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise RuntimeError(
                "Pinned run configuration changed; refusing to continue durable execution "
                f"(expected {expected}, got {actual})"
            )
    return payload.decode("utf-8")


def _load_mapping(source: Path) -> dict[str, Any]:
    data = yaml.safe_load(_read_source(source)) or {}
    if not isinstance(data, dict):
        raise ValueError("converge.yaml must contain a YAML mapping at the document root")
    flaky_ci_policy_from_mapping(data)
    selected = _select_model_profile_set(data)
    return _resolve_relative_paths(selected, source.parent)


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
    content = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    target = cfg.state_dir / _RUN_CONFIG_DIR / f"{run_id}-sha256-{digest}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(f"Run configuration snapshot already exists: {target}")

    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return cfg, target.resolve(), digest


def load_run_config_snapshot(path: str | Path, expected_sha256: str) -> ProjectConfig:
    """Load a pinned run configuration only when its durable content hash still matches."""
    source = Path(path).expanduser().resolve()
    path_digest = _snapshot_digest_from_path(source)
    if path_digest is None or path_digest != expected_sha256:
        raise RuntimeError(
            "Pinned run configuration metadata does not match its immutable snapshot path"
        )
    return load_config(source)
