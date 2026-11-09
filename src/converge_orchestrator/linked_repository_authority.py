"""REQ-REPO-005 deterministic linked-repository authority guard.

Pure offline primitive for REQ-REPO-005
(CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L281 [9. Repository modes, greenfield bootstrap,
and brownfield discovery]): nested Git repositories, submodules, linked worktrees, and external
mounts SHALL remain separate repository identities; internal-content edits SHALL require explicit
project authority rather than being implied by authority over the primary target repository.

``evaluate_linked_repository_authority`` returns fixed-order violation clauses:

* ``"separate_repository_identity_absorbed_into_primary"`` for a primary-attributed mutation
  inside a declared boundary;
* ``"linked_repository_edit_without_explicit_authority"`` for a boundary-internal edit attributed
  to a separate identity not listed in ``explicit_project_authority``.

Malformed boundary/mutation records, malformed state-field containers, and unsupported boundary
kinds fail closed; a non-mapping ``state`` raises ``TypeError``. No network/model/provider/
subprocess/git/filesystem side effects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_BOUNDARY_KINDS = frozenset(
    {"nested_git_repository", "submodule", "linked_worktree", "external_mount"}
)
_REQUIRED_BOUNDARY_FIELDS = frozenset({"kind", "path", "repository_identity"})
_REQUIRED_MUTATION_FIELDS = frozenset({"attributed_repository_identity", "content_path"})
_FIELD_PRIMARY = "primary_repository_identity"
_FIELD_EXPLICIT_AUTHORITY = "explicit_project_authority"
_FIELD_BOUNDARIES = "boundaries"
_FIELD_MUTATIONS = "mutations"


def _iter_records(value: object) -> tuple[object, ...] | None:
    """Return an iterable of records, or ``None`` for an invalid container."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return None
    return tuple(value)


def _normalize_path(path: object) -> str | None:
    """Return a normalized non-empty path string, or ``None`` for unusable values."""
    if isinstance(path, str):
        stripped = path.rstrip("/")
        if stripped:
            return stripped
    return None


def _contains(content_path: str, boundary_path: str) -> bool:
    """Return ``True`` when ``content_path`` is inside or equal to ``boundary_path``."""
    if content_path == boundary_path:
        return True
    return content_path.startswith(boundary_path + "/")


def _deepest_boundary(
    content_path: str, boundaries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the deepest containing boundary; tie-break on identity then kind."""
    containing = [b for b in boundaries if _contains(content_path, b["path"])]
    if not containing:
        return None
    containing.sort(key=lambda b: (-len(b["path"]), b["path"], b["identity"], b["kind"]))
    return containing[0]


def evaluate_linked_repository_authority(state: object) -> tuple[str, ...]:
    """Return REQ-REPO-005 authority clauses for ``state``; fail closed on malformed input."""
    if not isinstance(state, Mapping):
        raise TypeError("linked repository authority state must be a mapping")

    primary_identity = state.get(_FIELD_PRIMARY)

    raw_boundaries = _iter_records(state.get(_FIELD_BOUNDARIES))
    if raw_boundaries is None:
        return ("invalid_boundary_record",)

    raw_mutations = _iter_records(state.get(_FIELD_MUTATIONS))
    if raw_mutations is None:
        return ("invalid_mutation_record",)

    explicit_authority: set[str] = set()
    explicit_raw = _iter_records(state.get(_FIELD_EXPLICIT_AUTHORITY))
    if explicit_raw is not None:
        for item in explicit_raw:
            if isinstance(item, str):
                explicit_authority.add(item)

    boundaries: list[dict[str, Any]] = []
    for boundary in raw_boundaries:
        if not isinstance(boundary, Mapping) or not _REQUIRED_BOUNDARY_FIELDS.issubset(
            boundary.keys()
        ):
            return ("invalid_boundary_record",)
        kind = boundary.get("kind")
        path = _normalize_path(boundary.get("path"))
        identity = boundary.get("repository_identity")
        if not isinstance(kind, str) or path is None or not isinstance(identity, str):
            return ("invalid_boundary_record",)
        if kind not in _BOUNDARY_KINDS:
            return ("unsupported_boundary_kind",)
        boundaries.append({"kind": kind, "path": path, "identity": identity})

    mutations: list[tuple[str, str]] = []
    for mutation in raw_mutations:
        if not isinstance(mutation, Mapping) or not _REQUIRED_MUTATION_FIELDS.issubset(
            mutation.keys()
        ):
            return ("invalid_mutation_record",)
        attributed = mutation.get("attributed_repository_identity")
        content_path = _normalize_path(mutation.get("content_path"))
        if not isinstance(attributed, str) or content_path is None:
            return ("invalid_mutation_record",)
        mutations.append((attributed, content_path))

    absorbed = False
    unauthorized = False
    for attributed, content_path in mutations:
        deepest = _deepest_boundary(content_path, boundaries)
        if deepest is None:
            continue
        if attributed == primary_identity:
            absorbed = True
        elif deepest["identity"] not in explicit_authority:
            unauthorized = True

    clauses: list[str] = []
    if absorbed:
        clauses.append("separate_repository_identity_absorbed_into_primary")
    if unauthorized:
        clauses.append("linked_repository_edit_without_explicit_authority")
    return tuple(clauses)
