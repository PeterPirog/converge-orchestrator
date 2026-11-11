from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

from .shell import run

_WORKSPACE_MARKER_NAME = "converge-workspace-id"
_STATE_MARKER_NAME = ".converge-state-id"
_NESTED_REPO_MARKER = "REQ-REPO-005 nested repository authority denied"


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


def _nested_repository_root(target: Path, root: Path) -> Path | None:
    """Return the nearest ancestor between *target* and *root* that marks a Git boundary."""
    current = target
    while current != root and root in current.parents:
        git_entry = current / ".git"
        if git_entry.is_dir():
            return current
        if git_entry.is_file():
            try:
                text = git_entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return current
            if text.splitlines() and text.splitlines()[0].strip().startswith("gitdir:"):
                return current
        current = current.parent
    return None


def assert_nested_repository_authority(project: dict, repo: Path, target: Path) -> str:
    """Fail closed on nested Git boundary edits without explicit project authority."""
    assert_workspace_affinity(project, repo)
    root = repo.expanduser().resolve()
    resolved = target.expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise WorkspaceAffinityError(
            _NESTED_REPO_MARKER + ": target outside authorized repository root"
        )
    nested_root = _nested_repository_root(resolved, root)
    if nested_root is None:
        return mutation_domain_id(root)
    try:
        nested_id = mutation_domain_id(nested_root)
    except Exception as exc:
        raise WorkspaceAffinityError(_NESTED_REPO_MARKER) from exc
    authority = project.get("nested_repository_authority")
    if isinstance(authority, Mapping) and authority.get(nested_id) is True:
        return nested_id
    raise WorkspaceAffinityError(_NESTED_REPO_MARKER)
