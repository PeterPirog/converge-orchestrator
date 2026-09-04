from __future__ import annotations

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


def assert_workspace_affinity(project: dict, repo: Path) -> str:
    """Reject execution when a shared registry project belongs to another Git workspace."""
    actual = workspace_id(repo)
    expected = project.get("workspace_id")
    if expected and expected != actual:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} is bound to workspace {expected}, "
            f"but this controller sees workspace {actual}. Use a shared Git workspace or route "
            "the project to its bound worker/filesystem."
        )
    return actual


def assert_state_store_affinity(project: dict, state_dir: Path) -> str:
    """Reject execution when filesystem evidence/state is not the project's bound state store."""
    actual = state_store_id(state_dir)
    expected = project.get("state_store_id")
    if expected and expected != actual:
        raise WorkspaceAffinityError(
            f"Project {project.get('id', '<unknown>')} is bound to state store {expected}, "
            f"but this controller sees state store {actual}. Route the project to the worker "
            "that owns its state directory or mount the same durable state directory."
        )
    return actual
