from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

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
_DRIVE_ROOTED_RE = re.compile(r"^[A-Za-z]:/")


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
    if any(not isinstance(set_name, str) for set_name in profile_sets):
        raise ValueError("models.profile_sets keys must be strings")

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
            except ValidationError as exc:
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


def resolve_source_of_truth(data: dict[str, Any]) -> Any:
    """Resolve the single authoritative Markdown Source of Truth declared by ``data``.

    REQ-SOT-001: a run SHALL accept exactly one authoritative Source of Truth.
    This pure resolver enforces that contract over the raw configuration mapping:
    it returns the declared ``requirements_path`` when exactly one declaration is
    present (top-level or under ``project``), and fails closed with ``ValueError``
    when zero or multiple declarations are present. It performs no filesystem,
    network, model, git, or subprocess access and enforces no file-name format.
    """
    declarations: list[Any] = []
    if "requirements_path" in data:
        declarations.append(data["requirements_path"])
    project = data.get("project")
    if isinstance(project, dict) and "requirements_path" in project:
        declarations.append(project["requirements_path"])

    if len(declarations) != 1:
        if not declarations:
            raise ValueError(
                "a run must declare exactly one Source of Truth via requirements_path; "
                "none declared"
            )
        raise ValueError(
            "a run must declare exactly one Source of Truth via requirements_path; "
            f"{len(declarations)} declarations are ambiguous"
        )
    return declarations[0]


def _iter_path_list(value: Any) -> list[str]:
    """Normalize explicit run-state path roots, failing closed on absent, null, or malformed."""
    if value is None:
        raise ValueError("path roots must be an explicit list; absent or null roots fail closed")
    if isinstance(value, (str, os.PathLike)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("path roots must be a list of paths or a single path")
    for entry in value:
        if not isinstance(entry, (str, os.PathLike)):
            raise ValueError("path root entries must be strings or os.PathLike values")
    return [os.fspath(entry) for entry in value]


def _canonicalize_path(value: str) -> str:
    """Canonicalize an absolute path offline; fold case as a conservative over-approximation.

    Accept only fully-qualified absolute paths (POSIX ``/...`` or drive-rooted ``[A-Za-z]:/``);
    drive-relative, non-ASCII-drive, root-relative, and verbatim spellings fail closed.
    """
    if value.startswith(("\\", "//?/", "//./")):
        raise ValueError(f"path {value!r} must be a plain absolute path")
    normalized = posixpath.normpath(value.replace("\\", "/"))
    drive = bool(_DRIVE_ROOTED_RE.match(normalized))
    if not (normalized.startswith("/") or drive):
        raise ValueError(f"path {value!r} must be absolute; relative paths fail closed")
    return normalized.lower()


def _is_within(path: str, root: str) -> bool:
    """Return True when ``path`` sits at or inside ``root`` after canonicalization."""
    canonical_path = _canonicalize_path(path)
    canonical_root = _canonicalize_path(root)
    if canonical_root == "/":
        return canonical_path.startswith("/")
    return canonical_path == canonical_root or canonical_path.startswith(f"{canonical_root}/")


def evaluate_source_of_truth_isolation(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-SOT-002 over a run-state snapshot: the Source of Truth SHALL reside outside
    every candidate worktree and SHALL NOT be modifiable by any model-backed role. Returns the
    violated clauses in fixed order (candidate worktree first, then model-role-writable); a
    missing source path defers to REQ-SOT-001, and malformed or relative paths fail closed.
    """
    if "source_of_truth_path" not in state or state["source_of_truth_path"] is None:
        return ()
    raw_source = state["source_of_truth_path"]
    if not isinstance(raw_source, (str, os.PathLike)) or not os.fspath(raw_source):
        raise ValueError("source_of_truth_path must be a non-empty string or os.PathLike")
    source = _canonicalize_path(os.fspath(raw_source))

    clauses: list[str] = []
    candidate_roots = _iter_path_list(state.get("candidate_worktree_paths"))
    if any(_is_within(source, root) for root in candidate_roots):
        clauses.append("source_of_truth_inside_candidate_worktree")
    writable_roots = _iter_path_list(state.get("model_role_writable_paths"))
    if any(_is_within(source, root) for root in writable_roots):
        clauses.append("source_of_truth_model_role_writable")
    return tuple(clauses)


def evaluate_source_of_truth_pin(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-SOT-010 over a run-state snapshot.

    REQ-SOT-010: a run SHALL pin the authoritative Source-of-Truth content digest
    before repository mutation. This pure, offline, deterministic guard inspects a
    plain run-state mapping with the keys ``pinned_source_of_truth_sha256``,
    ``current_source_of_truth_sha256``, and ``repository_mutation_started``, and
    returns the violated clauses in fixed order:

    * ``source_of_truth_pin_missing_before_mutation`` when no Source-of-Truth
      content digest was pinned, so repository mutation must not proceed;
    * ``source_of_truth_pin_not_authoritative`` when a pinned digest exists but
      does not equal the authoritative current Source-of-Truth content digest.

    It returns an empty tuple only when a digest matching the authoritative current
    Source-of-Truth content was pinned before repository mutation. It performs no
    filesystem, network, model, git, or subprocess access.
    """
    pinned = state.get("pinned_source_of_truth_sha256")
    current = state.get("current_source_of_truth_sha256")

    if pinned is None:
        return ("source_of_truth_pin_missing_before_mutation",)
    if pinned != current:
        return ("source_of_truth_pin_not_authoritative",)
    return ()


def evaluate_source_of_truth_drift(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-SOT-011 over a run-state snapshot.

    REQ-SOT-011: a run SHALL stop mutation and integration when the authoritative
    Source-of-Truth content no longer matches its pinned digest. This pure, offline,
    deterministic guard inspects a plain run-state mapping with the keys
    ``pinned_source_of_truth_sha256`` and ``current_source_of_truth_sha256``, and
    returns the violated halt clauses in fixed order (mutation first, then
    integration):

    * ``source_of_truth_drift_halt_mutation`` when the authoritative Source-of-Truth
      content no longer matches its pinned digest, so repository mutation must stop;
    * ``source_of_truth_drift_halt_integration`` when the same drift condition holds,
      so integration must stop.

    It returns an empty tuple when a pinned digest matches the authoritative current
    Source-of-Truth content digest, and also when no digest was pinned: a missing pin
    is REQ-SOT-010's concern (enforced by ``evaluate_source_of_truth_pin``), not drift.
    It performs no filesystem, network, model, git, or subprocess access.
    """
    pinned = state.get("pinned_source_of_truth_sha256")
    current = state.get("current_source_of_truth_sha256")

    if pinned is None or pinned == current:
        return ()
    return (
        "source_of_truth_drift_halt_mutation",
        "source_of_truth_drift_halt_integration",
    )


def _load_mapping(source: Path) -> dict[str, Any]:
    data = yaml.safe_load(_read_source(source)) or {}
    if not isinstance(data, dict):
        raise ValueError("converge.yaml must contain a YAML mapping at the document root")
    flaky_ci_policy_from_mapping(data)
    resolve_source_of_truth(data)
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
