from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
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


def _git_worktree_root(repo: Path) -> Path:
    try:
        result = run(["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=30)
    except OSError as exc:
        raise WorkspaceAffinityError(
            f"Unable to resolve Git worktree root for {repo}: {exc}"
        ) from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkspaceAffinityError(f"Unable to resolve Git worktree root for {repo}")
    return Path(result.stdout.strip()).expanduser().resolve()


def _find_governing_git_root(path: Path) -> Path | None:
    """Return the innermost Git worktree root that governs ``path``."""
    current = path.resolve()
    if not current.is_dir():
        current = current.parent
    while True:
        marker = current / ".git"
        if marker.exists():
            root = _git_worktree_root(current)
            if root != current:
                raise WorkspaceAffinityError(f"Ambiguous .git marker at {current}")
            return root
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _workspace_identity(common_dir: Path, worktree_root: Path) -> str:
    key = f"converge:workspace:{common_dir.as_posix()}:{worktree_root.as_posix()}"
    return f"wsid-{hashlib.sha256(key.encode()).hexdigest()}"


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


def resolve_workspace_identity(repo: Path, target: Path) -> dict[str, Any]:
    try:
        primary_worktree = _git_worktree_root(repo.expanduser().resolve())
        primary_common = _git_common_dir(primary_worktree)
        primary_identity = _workspace_identity(primary_common, primary_worktree)
        governing = _find_governing_git_root(target.expanduser().resolve())
        if governing is None:
            raise WorkspaceAffinityError(
                f"Target {target.expanduser().resolve()} is not inside any Git repository"
            )
        target_common = _git_common_dir(governing)
        target_identity = _workspace_identity(target_common, governing)
    except OSError as exc:
        raise WorkspaceAffinityError(f"Workspace identity resolution failed: {exc}") from exc
    if target_identity == primary_identity:
        return {"identity": primary_identity, "distinct_identity": None}
    reason = (
        "linked_worktree" if target_common == primary_common
        else "submodule" if target_common.is_relative_to(primary_common)
        else "external_mount" if not governing.is_relative_to(primary_worktree)
        else "nested_repository"
    )
    distinct = {"reason": reason, "identity": target_identity}
    return {"identity": target_identity, "distinct_identity": distinct}
