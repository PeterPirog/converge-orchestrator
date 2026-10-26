from __future__ import annotations

import hashlib
import math
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
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:")


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


def _is_nonempty_string(value: Any) -> bool:
    """Return True only when ``value`` is a non-empty, non-blank string, else False."""
    return isinstance(value, str) and value.strip() != ""


def evaluate_source_of_truth_change_requires_new_run(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-SOT-006 over a run-state snapshot.

    REQ-SOT-006: material Source-of-Truth changes SHALL require a new run identity and
    a new pinned digest rather than modifying the contract of an active run. This pure,
    offline, deterministic guard inspects a plain run-state mapping with the keys
    ``source_of_truth_change_is_material``, ``active_run_identity``,
    ``proposed_run_identity``, ``pinned_source_of_truth_sha256``, and
    ``current_source_of_truth_sha256``, returning the violated clauses in fixed order:
    ``source_of_truth_change_requires_new_run_identity`` then
    ``source_of_truth_change_requires_new_pinned_digest``.

    It fails closed: an absent, null, or non-boolean materiality value raises
    ``ValueError``; absent, blank, non-string, or mismatched identities or digests emit
    the clause. It returns an empty tuple for a non-material change, or a material
    change properly separated under a new run with a pin matching the changed digest.
    It performs no filesystem, network, model, git, or subprocess access.
    """
    material = state.get("source_of_truth_change_is_material")
    if not isinstance(material, bool):
        raise ValueError(
            "source_of_truth_change_is_material must be an explicit boolean; "
            "absent, null, or malformed materiality fails closed"
        )
    if material is False:
        return ()

    clauses: list[str] = []
    active = state.get("active_run_identity")
    proposed = state.get("proposed_run_identity")
    if not _is_nonempty_string(active) or not _is_nonempty_string(proposed) or proposed == active:
        clauses.append("source_of_truth_change_requires_new_run_identity")

    pinned = state.get("pinned_source_of_truth_sha256")
    current = state.get("current_source_of_truth_sha256")
    if not _is_nonempty_string(pinned) or not _is_nonempty_string(current) or pinned != current:
        clauses.append("source_of_truth_change_requires_new_pinned_digest")
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


def evaluate_mandatory_convergence_blocking(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-SOT-012 over a run-state snapshot.

    Mandatory requirement records SHALL block convergence until their compliance
    status is exactly ``pass``. This pure, offline guard reads ``requirements``
    (a stable-ordered list of records, each a mapping with a non-empty string
    ``id`` and optional ``severity``) and ``compliance``. ``compliance`` may be a
    flat mapping of record id to an entry mapping carrying ``status``, or a
    snapshot mapping whose ``entries`` value is that flat mapping. It returns one
    ``mandatory_requirement_blocks_convergence:<id>`` clause per blocking mandatory
    record, in stable record order. Only severity exactly ``recommended`` is exempt;
    absent or unknown severity is mandatory (fail closed). A missing compliance
    entry blocks (no exemption). A missing or non-list ``requirements``, a missing
    or non-mapping ``compliance`` or ``compliance.entries``, a non-mapping record,
    a record without a non-empty string id, or a non-mapping entry fails closed with
    ``ValueError``. No filesystem, network, model, git, or subprocess access is
    performed.
    """
    if not isinstance(state, dict):
        raise ValueError("run-state must be a mapping")
    if "requirements" not in state:
        raise ValueError("requirements must be an explicit list; absent requirements fail closed")
    requirements = state["requirements"]
    if not isinstance(requirements, list):
        raise ValueError("requirements must be a list of requirement records")
    if "compliance" not in state:
        raise ValueError("compliance must be an explicit mapping; absent compliance fails closed")
    compliance = state["compliance"]
    if not isinstance(compliance, dict):
        raise ValueError("compliance must be a compliance snapshot mapping")
    if "entries" in compliance:
        entries = compliance["entries"]
        if not isinstance(entries, dict):
            raise ValueError("compliance.entries must be a mapping of record id to entry")
    else:
        entries = compliance
    for entry_key, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"compliance entry {entry_key!r} must be a mapping")

    clauses: list[str] = []
    for record in requirements:
        if not isinstance(record, dict):
            raise ValueError("requirement records must be mappings")
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("requirement records must carry a non-empty string id")
        if record.get("severity") == "recommended":
            continue
        entry = entries.get(record_id)
        status = entry.get("status") if isinstance(entry, dict) else None
        if status != "pass":
            clauses.append(f"mandatory_requirement_blocks_convergence:{record_id}")
    return tuple(clauses)


_CONTROL_PLANE_ARTIFACTS = (
    ("project_config_path", "project_config_inside_candidate_writer_root"),
    ("pinned_run_snapshot_path", "pinned_run_snapshot_inside_candidate_writer_root"),
    ("source_of_truth_path", "source_of_truth_inside_candidate_writer_root"),
    ("secret_sources", "secret_source_inside_candidate_writer_root"),
    ("durable_control_state_path", "durable_control_state_inside_candidate_writer_root"),
)


def evaluate_control_plane_authority(state: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-CONFIG-012 over a run-state snapshot.

    REQ-CONFIG-012: the authoritative project-configuration source, pinned run
    snapshots, Source of Truth, secret sources, and durable control state SHALL
    remain outside candidate-writer mutation authority; any repository-tracked
    copy of control-plane configuration SHALL be non-authoritative unless a later
    run explicitly adopts it through trusted project configuration.

    This pure, offline, deterministic guard inspects a plain run-state mapping
    with the keys ``project_config_path``, ``pinned_run_snapshot_path``,
    ``source_of_truth_path``, ``secret_sources``, ``durable_control_state_path``,
    ``candidate_writer_roots``, and optionally
    ``repository_tracked_control_plane_copy`` (a mapping carrying ``path`` and
    ``adopted``), and returns the violated clauses in fixed order:

    * ``project_config_inside_candidate_writer_root``
    * ``pinned_run_snapshot_inside_candidate_writer_root``
    * ``source_of_truth_inside_candidate_writer_root``
    * ``secret_source_inside_candidate_writer_root``
    * ``durable_control_state_inside_candidate_writer_root``

    when the corresponding control-plane artifact sits at or inside any
    candidate-writer mutation root, reported in that fixed artifact order, then

    * ``repository_tracked_control_plane_copy_is_authoritative`` when a
      repository-tracked copy of control-plane configuration is declared with
      ``adopted`` false, so it is treated as authoritative without explicit
      adoption through trusted project configuration.

    It fails closed with ``ValueError`` on a missing control-plane path
    declaration, missing or empty candidate-writer roots, or a malformed or
    relative path, so isolation and adoption cannot be silently verified away.
    It performs no filesystem, network, model, git, or subprocess access.
    """
    if not isinstance(state, dict):
        raise ValueError("run-state must be a mapping")

    candidate_roots = _iter_path_list(state.get("candidate_writer_roots"))
    if not candidate_roots:
        raise ValueError(
            "candidate_writer_roots must be an explicit non-empty list of paths; "
            "absent or empty roots fail closed"
        )
    for root in candidate_roots:
        _canonicalize_path(root)

    clauses: list[str] = []
    for key, clause in _CONTROL_PLANE_ARTIFACTS:
        if key not in state or state[key] is None:
            raise ValueError(f"{key} must be declared; a missing control-plane path fails closed")
        raw = state[key]
        paths = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        if not paths:
            raise ValueError(
                f"{key} must declare at least one path; "
                "an empty declaration fails closed"
            )
        normalized_paths: list[str] = []
        for path in paths:
            if not isinstance(path, (str, os.PathLike)) or not os.fspath(path):
                raise ValueError(
                    f"{key} entries must be non-empty strings or os.PathLike values"
                )
            normalized = os.fspath(path)
            _canonicalize_path(normalized)
            normalized_paths.append(normalized)
        if any(_is_within(path, root) for path in normalized_paths for root in candidate_roots):
            clauses.append(clause)

    copy = state.get("repository_tracked_control_plane_copy")
    if copy is not None:
        if not isinstance(copy, dict):
            raise ValueError("repository_tracked_control_plane_copy must be a mapping")
        copy_path = copy.get("path")
        if not isinstance(copy_path, (str, os.PathLike)) or not os.fspath(copy_path):
            raise ValueError(
                "repository_tracked_control_plane_copy.path must be a "
                "non-empty string or os.PathLike"
            )
        _canonicalize_path(os.fspath(copy_path))
        adopted = copy.get("adopted")
        if not isinstance(adopted, bool):
            raise ValueError(
                "repository_tracked_control_plane_copy.adopted must be an explicit boolean; "
                "absent, null, or malformed adoption fails closed"
            )
        if not adopted:
            clauses.append("repository_tracked_control_plane_copy_is_authoritative")

    return tuple(clauses)


_PROVIDER_ENDPOINT_CATEGORIES = ("model", "gateway", "mcp", "external_analysis")
_TRUSTED_PROVIDER_CLASSES = {"local", "trusted"}


def evaluate_provider_data_egress(policy: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-CONFIG-014 over a project-configuration mapping.

    REQ-CONFIG-014: project configuration SHALL define provider trust classes and
    permitted repository-data egress for model, gateway, MCP, and external analysis
    endpoints whose invocation can transmit project content.

    This pure, offline, deterministic guard inspects the ``provider_trust`` mapping
    (a mapping of endpoint category to a declaration mapping carrying
    ``trust_class`` and ``repository_data_egress``) and returns the violated
    clauses in fixed order (model, gateway, mcp, external_analysis; within a
    category, the trust-class clause before the egress clause):

    * ``provider_trust_class_undeclared:<category>`` when the category's trust
      class is not explicitly declared (absent, null, non-string, or blank);
    * ``repository_data_egress_undeclared:<category>`` when the category's
      repository-data egress is not explicitly declared (absent, null, or
      non-boolean);
    * ``repository_data_egress_permitted_to_untrusted_provider:<category>`` when
      the category permits repository-data egress while its declared trust class
      is neither local nor trusted.

    It fails closed with ``ValueError`` on a missing or empty ``provider_trust``
    mapping, a missing or unknown endpoint category, or a non-mapping category
    declaration, so a trust/egress gap cannot be silently verified away. It
    performs no filesystem, network, model, git, or subprocess access.
    """
    if not isinstance(policy, dict):
        raise ValueError("project configuration must be a mapping")

    provider_trust = policy.get("provider_trust")
    if not isinstance(provider_trust, dict) or not provider_trust:
        raise ValueError(
            "provider_trust must be a non-empty mapping of endpoint categories; "
            "a missing or empty declaration fails closed"
        )

    missing = [
        category for category in _PROVIDER_ENDPOINT_CATEGORIES if category not in provider_trust
    ]
    if missing:
        raise ValueError(
            "provider_trust must declare every endpoint category; missing: "
            f"{missing}; allowed: {list(_PROVIDER_ENDPOINT_CATEGORIES)}"
        )
    unknown = [key for key in provider_trust if key not in _PROVIDER_ENDPOINT_CATEGORIES]
    if unknown:
        raise ValueError(
            f"provider_trust declares unknown endpoint categories: {unknown}; "
            f"allowed: {list(_PROVIDER_ENDPOINT_CATEGORIES)}"
        )

    for category in _PROVIDER_ENDPOINT_CATEGORIES:
        if not isinstance(provider_trust[category], dict):
            raise ValueError(
                f"provider_trust.{category} must be a mapping declaring trust_class "
                "and repository_data_egress"
            )

    clauses: list[str] = []
    for category in _PROVIDER_ENDPOINT_CATEGORIES:
        declaration = provider_trust[category]
        trust_class = declaration.get("trust_class")
        egress = declaration.get("repository_data_egress")

        trust_declared = isinstance(trust_class, str) and trust_class.strip() != ""
        egress_declared = isinstance(egress, bool)

        if not trust_declared:
            clauses.append(f"provider_trust_class_undeclared:{category}")
        if not egress_declared:
            clauses.append(f"repository_data_egress_undeclared:{category}")
        elif egress is True and trust_declared and trust_class not in _TRUSTED_PROVIDER_CLASSES:
            clauses.append(
                f"repository_data_egress_permitted_to_untrusted_provider:{category}"
            )
    return tuple(clauses)


_RETENTION_MODES = {"finite_horizon", "durable_archival"}


def _is_finite_positive_number(value: Any) -> bool:
    """Return True only when ``value`` is a finite, positive real number (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return value > 0  # All ints are finite; float() would overflow on huge ints.
    return math.isfinite(value) and value > 0


def _requires_durable_retention(declaration: Any) -> bool:
    """Return True only when an evidence-class/archive declaration is a mapping that
    requires durable capture, immutable retention proof, or both. Malformed or
    under-declared declarations fail closed to False."""
    if not isinstance(declaration, dict):
        return False
    return (
        declaration.get("durable_capture") is True
        or declaration.get("immutable_retention_proof") is True
    )


def evaluate_release_evidence_retention(policy: dict[str, Any]) -> tuple[str, ...]:
    """Enforce REQ-CONFIG-017 over a project-configuration mapping.

    REQ-CONFIG-017: the project configuration SHALL define a finite release-evidence
    retention horizon or an explicitly durable archival policy, and SHALL define which
    external evidence classes and released repository-object archives require durable
    capture, immutable retention proof, or both before they can contribute to release
    convergence.

    This pure, offline, deterministic guard inspects the ``release_evidence_retention``
    section of a project-configuration mapping and returns the violated clauses in fixed
    order (retention mode first, then external evidence classes, then released
    repository-object archives):

    * ``retention_mode_undeclared`` when ``retention_mode`` is absent, blank, or not one
      of the two declared modes (``finite_horizon`` or ``durable_archival``);
    * ``retention_horizon_invalid`` when the mode is ``finite_horizon`` but
      ``retention_horizon`` is absent, zero, negative, non-numeric, or non-finite;
    * ``durable_archival_policy_undeclared`` when the mode is ``durable_archival`` but
      ``durable_archival_policy`` is absent, blank, or non-string;
    * ``external_evidence_classes_undeclared`` when ``external_evidence_classes`` is
      absent or empty; otherwise ``external_evidence_class_retention_undeclared:<name>``
      for each class whose name is not a non-empty string, or whose declaration is
      malformed or requires neither durable capture nor immutable retention proof;
    * ``released_repository_object_archives_undeclared`` when
      ``released_repository_object_archives`` is absent or empty; otherwise
      ``released_repository_object_archive_retention_undeclared:<name>`` for each archive
      whose name is not a non-empty string, or whose declaration is malformed or
      requires neither durable capture nor immutable retention proof.

    A complete policy declaring a finite positive retention horizon (or an explicit
    durable archival policy) plus a complete durable-capture / immutable-retention-proof
    requirement for every external evidence class and released repository-object archive
    is accepted with no violated clauses. Requiring durable capture alone, or immutable
    retention proof alone, satisfies the "durable capture, immutable retention proof, or
    both" contract. It fails closed with ``ValueError`` on a missing or non-mapping
    ``release_evidence_retention`` section so an under-declared retention policy cannot be
    silently verified away. It performs no filesystem, network, model, git, or subprocess
    access.
    """
    if not isinstance(policy, dict):
        raise ValueError("project configuration must be a mapping")
    section = policy.get("release_evidence_retention")
    if not isinstance(section, dict):
        raise ValueError(
            "release_evidence_retention must be a mapping declaring the retention mode, "
            "external evidence classes, and released repository-object archives; "
            "an absent retention section fails closed"
        )

    clauses: list[str] = []

    # Retention mode: a finite horizon or an explicitly durable archival policy.
    mode = section.get("retention_mode")
    if not isinstance(mode, str) or mode not in _RETENTION_MODES:
        clauses.append("retention_mode_undeclared")
    elif mode == "finite_horizon":
        if not _is_finite_positive_number(section.get("retention_horizon")):
            clauses.append("retention_horizon_invalid")
    else:  # durable_archival
        if not _is_nonempty_string(section.get("durable_archival_policy")):
            clauses.append("durable_archival_policy_undeclared")

    # External evidence classes.
    evidence_classes = section.get("external_evidence_classes")
    if not isinstance(evidence_classes, dict) or not evidence_classes:
        clauses.append("external_evidence_classes_undeclared")
    else:
        for name, declaration in evidence_classes.items():
            if not _is_nonempty_string(name) or not _requires_durable_retention(declaration):
                clauses.append(f"external_evidence_class_retention_undeclared:{name}")

    # Released repository-object archives.
    archives = section.get("released_repository_object_archives")
    if not isinstance(archives, dict) or not archives:
        clauses.append("released_repository_object_archives_undeclared")
    else:
        for name, declaration in archives.items():
            if not _is_nonempty_string(name) or not _requires_durable_retention(declaration):
                clauses.append(
                    f"released_repository_object_archive_retention_undeclared:{name}"
                )

    return tuple(clauses)


def _plain_path_shape(value: str) -> str:
    """Classify a plain path as ``absolute`` or ``relative``; malformed spellings fail closed.

    POSIX ``/...`` and drive-rooted ``[A-Za-z]:/...`` spellings are absolute; verbatim,
    ``//?/``/``//./``, drive-relative, and drive-rooted spellings that lose their drive root
    under normalization (e.g. ``C:foo/../bar``) fail closed with ``ValueError``; the
    remaining plain spellings are relative. No filesystem access occurs.
    """
    if value.startswith(("\\", "//?/", "//./")):
        raise ValueError(f"path {value!r} must be a plain absolute or relative path")
    slash = value.replace("\\", "/")
    if _DRIVE_RELATIVE_RE.match(slash):
        normalized = posixpath.normpath(slash)
        if not _DRIVE_ROOTED_RE.match(slash) or not (
            normalized.startswith("/") or _DRIVE_ROOTED_RE.match(normalized)
        ):
            raise ValueError(f"path {value!r} must be a plain absolute or relative path")
        return "absolute"
    if posixpath.normpath(slash).startswith("/"):
        return "absolute"
    return "relative"


_PINNED_AUTHORITY_KINDS = {"pinned_project_config", "pinned_authority_source"}


def evaluate_canonical_durable_control_state_paths(declaration: Any) -> tuple[str, ...]:
    """Enforce REQ-CONFIG-018 over one durable control-state path declaration.

    The canonical absolute identity SHALL come from pinned project configuration or another
    explicitly pinned authority source and SHALL NOT depend on process CWD. This pure, offline,
    deterministic guard inspects ``durable_control_state_path``, ``pinned_authority_source``
    (a pinned ``kind`` plus an absolute ``base``), and ``process_cwd``; it returns the violated
    clauses in fixed order: ``pinned_authority_source_undeclared`` when that source is missing,
    unpinned, blank-kind, or non-absolute-base (required for both absolute and relative
    declarations), and ``control_state_path_depends_on_process_cwd`` when a relative declaration
    is anchored on the process CWD (including a canonical re-spelling). It fails closed with
    ``ValueError`` for a missing or empty path, a malformed path value (verbatim, ``//?/``/
    ``//./``, drive-relative, or a drive-qualified spelling that does not remain absolute), or
    a missing or non-absolute ``process_cwd``; it performs no filesystem, network, model, git,
    or subprocess access.
    """
    if not isinstance(declaration, dict):
        raise ValueError("durable control-state declaration must be a mapping")

    raw_path = declaration.get("durable_control_state_path")
    if not isinstance(raw_path, (str, os.PathLike)) or not os.fspath(raw_path).strip():
        raise ValueError("durable_control_state_path must be a non-empty string path")
    path = os.fspath(raw_path)

    raw_cwd = declaration.get("process_cwd")
    if not isinstance(raw_cwd, (str, os.PathLike)) or not os.fspath(raw_cwd).strip():
        raise ValueError("process_cwd must be a non-empty string path")
    canonical_cwd = _canonicalize_path(os.fspath(raw_cwd))

    # Validate the pinned, non-CWD authority source for every declaration, absolute or relative.
    authority = declaration.get("pinned_authority_source")
    kind = authority.get("kind") if isinstance(authority, dict) else None
    base = authority.get("base") if isinstance(authority, dict) else None
    canonical_base: str | None = None
    if (
        isinstance(kind, str)
        and kind in _PINNED_AUTHORITY_KINDS
        and isinstance(base, (str, os.PathLike))
        and os.fspath(base)
    ):
        try:
            canonical_base = _canonicalize_path(os.fspath(base))
        except ValueError:
            canonical_base = None
    if canonical_base is None:
        return ("pinned_authority_source_undeclared",)

    if _plain_path_shape(path) == "absolute":
        _canonicalize_path(path)
        return ()

    if canonical_base == canonical_cwd:
        return ("control_state_path_depends_on_process_cwd",)
    return ()


def _contains_secret_material(value: Any, secret_values: frozenset[str]) -> bool:
    if isinstance(value, dict):
        return any(_contains_secret_material(item, secret_values) for item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_material(item, secret_values) for item in value)
    if isinstance(value, str):
        return any(secret in value for secret in secret_values)
    return any(secret in str(value) for secret in secret_values)


def evaluate_shared_control_state_backend_identity(declaration: Any) -> tuple[str, ...]:
    """Enforce REQ-CONFIG-019: record the shared control-state backend identity secret-safe.
    Fixed clauses in order: backend_kind_undeclared, logical_database_or_namespace_undeclared,
    authority_generation_invalid, stable_deployment_identity_invalid,
    secret_material_in_backend_identity; connection_material holds secrets (substring); offline."""
    if not isinstance(declaration, dict):
        raise ValueError("declaration must be a mapping")
    material = declaration.get("connection_material")
    if not isinstance(material, dict) or not material or not all(
        _is_nonempty_string(k) and _is_nonempty_string(v) for k, v in material.items()
    ):
        raise ValueError("connection_material must be a non-empty mapping of non-empty strings")
    recorded = declaration.get("recorded_backend_identity")
    if not isinstance(recorded, dict) or not recorded:
        raise ValueError("recorded_backend_identity must be a non-empty mapping")
    secret_values = frozenset(material.values())
    clauses: list[str] = []
    if not _is_nonempty_string(declaration.get("backend_kind")):
        clauses.append("backend_kind_undeclared")
    if not _is_nonempty_string(declaration.get("logical_database_or_namespace")):
        clauses.append("logical_database_or_namespace_undeclared")
    generation = declaration.get("authority_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        clauses.append("authority_generation_invalid")
    if "stable_deployment_identity" in declaration and not _is_nonempty_string(
        declaration["stable_deployment_identity"]
    ):
        clauses.append("stable_deployment_identity_invalid")
    if _contains_secret_material(recorded, secret_values):
        clauses.append("secret_material_in_backend_identity")
    return tuple(clauses)


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
