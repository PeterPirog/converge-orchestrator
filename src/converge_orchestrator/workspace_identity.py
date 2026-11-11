from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from .shell import run

_WORKSPACE_MARKER_NAME = "converge-workspace-id"
_STATE_MARKER_NAME = ".converge-state-id"


class WorkspaceAffinityError(RuntimeError):
    pass


def _git_common_dir(repo: Path) -> Path:
    result = run(["git", "rev-parse", "--git-common-dir"], cwd=repo, timeout=30)
    if result.returncode != 0:
        raise WorkspaceAffinityError(
            f"Unable to resolve Git common directory for {repo}: {result.stdout.strip()}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise WorkspaceAffinityError(f"Git returned an empty common directory for {repo}")
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.expanduser().resolve()


def _validate_id(raw: str, marker: Path, label: str) -> str:
    value = raw.strip()
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise WorkspaceAffinityError(f"Invalid Converge {label} marker: {marker}") from exc
    if normalized != value.lower():
        raise WorkspaceAffinityError(f"Non-canonical Converge {label} marker: {marker}")
    return normalized


def _marker_id(marker: Path, label: str) -> str:
    try:
        return _validate_id(marker.read_text(encoding="utf-8"), marker, label)
    except FileNotFoundError:
        pass

    candidate = str(uuid4())
    marker.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        return _validate_id(marker.read_text(encoding="utf-8"), marker, label)

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(candidate + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            marker.unlink(missing_ok=True)
        finally:
            raise
    return candidate


def workspace_id(repo: Path) -> str:
    """Return a clone-local UUID stored in Git metadata, never in tracked repository files."""
    root = repo.expanduser().resolve()
    return _marker_id(_git_common_dir(root) / _WORKSPACE_MARKER_NAME, "workspace")


def state_store_id(state_dir: Path) -> str:
    """Return an ID shared only by controllers that see the same durable state directory."""
    root = state_dir.expanduser().resolve()
    return _marker_id(root / _STATE_MARKER_NAME, "state-store")


def _canonical_identity_source(value: str) -> str:
    """Normalize a Git origin URL or local path into a stable identity source.

    Cosmetic differences that must not split one repository into two identities
    (backslash vs. forward slash, drive-letter case, a trailing separator, an
    upper-cased scheme or host) are canonicalized away.
    """
    value = value.strip()
    if "://" in value:
        scheme, rest = value.split("://", 1)
        if "/" in rest:
            host, tail = rest.split("/", 1)
            return f"{scheme.lower()}://{host.lower()}/{tail}"
        return f"{scheme.lower()}://{rest.lower()}"
    path = value.replace("\\", "/")
    if len(path) >= 3 and path[1] == ":" and path[2] == "/":
        path = path[0].lower() + path[1:]
    return path.rstrip("/")


def mutation_domain_id(repo: Path) -> str:
    """Return one canonical repository mutation domain identity (INV-MUTATION-001).

    The identity is equal for every independent clone and linked worktree of the
    same repository and distinct across unrelated repositories, independent of
    project ID, clone path, worktree path, or state store.

    It is derived only from local Git metadata — the canonical ``remote.origin.url``
    when present, otherwise the resolved Git common directory — and never performs
    network, model, provider, or fetch operations.
    """
    root = repo.expanduser().resolve()
    result = run(["git", "config", "--get", "remote.origin.url"], cwd=root, timeout=30)
    origin = result.stdout.strip() if result.returncode == 0 else ""
    source = origin if origin else _git_common_dir(root).as_posix()
    normalized = _canonical_identity_source(source)
    digest = hashlib.sha256(f"converge:mutation-domain:{normalized}".encode()).hexdigest()
    return f"mutdom-{digest}"


def assert_workspace_affinity(project: dict, repo: Path) -> str:
    """Reject execution unless the project is explicitly bound to this Git workspace."""
    actual = workspace_id(repo)
    expected = project.get("workspace_id")
    if not expected:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} has no workspace binding. "
            "Re-register the project once before execution or automatic recovery."
        )
    if expected != actual:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} is bound to workspace {expected}, "
            f"but this controller sees workspace {actual}. Use a shared Git workspace or route "
            "the project to its bound worker/filesystem."
        )
    return actual


def assert_state_store_affinity(project: dict, state_dir: Path) -> str:
    """Reject execution unless evidence/state is explicitly bound to this durable state store."""
    actual = state_store_id(state_dir)
    expected = project.get("state_store_id")
    if not expected:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} has no state-store binding. "
            "Re-register the project once before execution or automatic recovery."
        )
    if expected != actual:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} is bound to state store {expected}, "
            f"but this controller sees state store {actual}. Route the project to the worker "
            "that owns its state directory or mount the same durable state directory."
        )
    return actual


def _is_inside_or_equal(child: Path, parent: Path) -> bool:
    """Return True when ``child`` is ``parent`` or a descendant of ``parent``."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_repository_identity(path: Path) -> str:
    """Return the deterministic Git repository identity that owns ``path``.

    The identity is equal for every independent clone and linked worktree of the
    same repository and distinct across unrelated repositories, submodules,
    foreign worktrees, or external repository mounts. It is derived only from
    local Git metadata (the resolved Git common directory) and never performs
    network, model, provider, or fetch operations.
    """
    target = path.expanduser().resolve()
    cwd = target.parent if target.is_file() else target
    return _git_common_dir(cwd).as_posix()


def _find_nested_root(
    primary_root: Path, path_logical: Path, primary_identity: str
) -> Path | None:
    """Return the topmost ancestor of ``path_logical`` that is a separate identity.

    The returned path is still expressed inside ``primary_root`` (e.g. the mount
    point of an external repository), so it can be matched against project
    ``allowed_paths``.
    """
    current = path_logical
    if current.is_file():
        current = current.parent
    nested_root: Path | None = None
    while _is_inside_or_equal(current, primary_root) and current != primary_root:
        try:
            current_identity = resolve_repository_identity(current)
        except WorkspaceAffinityError:
            current_identity = None
        if current_identity is not None and current_identity != primary_identity:
            nested_root = current
        elif current_identity == primary_identity:
            break
        current = current.parent
    return nested_root


def assert_nested_repository_authority(
    project: dict, repo_root: Path, path: Path
) -> None:
    """Reject editing ``path`` unless it belongs to the primary identity or is explicitly named.

    REQ-REPO-005: nested Git repositories, submodules, linked worktrees, and
    external repository mounts remain separate repository identities. Editing
    their internal contents requires the project ``allowed_paths`` to explicitly
    name the nested root rather than being implied by authority over the primary
    target repository.

    This predicate fails closed: a missing ``allowed_paths`` or a path whose
    repository identity differs from ``repo_root`` and is not covered by the
    project's ``allowed_paths`` raises ``WorkspaceAffinityError``.
    """
    primary_root = repo_root.expanduser().resolve()
    primary_identity = resolve_repository_identity(primary_root)

    # Use the logical (unresolved) path when computing the location inside the
    # primary repository so external mounts and junctions are referenced by
    # their mount point, which is what a project would name in allowed_paths.
    target_logical = path.expanduser()
    if not _is_inside_or_equal(target_logical, primary_root):
        target_logical = target_logical.resolve()

    target_identity = resolve_repository_identity(target_logical)
    if primary_identity == target_identity:
        return

    try:
        rel_path = target_logical.relative_to(primary_root).as_posix()
    except ValueError as exc:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} may not edit {path}: "
            f"separate repository identity outside {repo_root} is not named in allowed_paths."
        ) from exc

    allowed_paths = project.get("allowed_paths", [])
    if not allowed_paths:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} has no allowed_paths; "
            f"editing separate repository identity at {path} is not authorized."
        )

    if any(fnmatch.fnmatch(rel_path, pattern) for pattern in allowed_paths):
        return

    # Also allow the nested repository root itself to be the named authority
    # target, so a project may name the root directory or a glob over it.
    nested_root = _find_nested_root(primary_root, target_logical, primary_identity)
    if nested_root is not None:
        nested_rel = nested_root.relative_to(primary_root).as_posix()
        if any(fnmatch.fnmatch(nested_rel, pattern) for pattern in allowed_paths):
            return
        nested_prefix = f"{nested_rel}/**"
        if any(fnmatch.fnmatch(nested_prefix, pattern) for pattern in allowed_paths):
            return

    raise WorkspaceAffinityError(
        f"Project {project.get('id', '<unknown>')} is not authorized to edit "
        f"separate repository identity at {path}."
    )
