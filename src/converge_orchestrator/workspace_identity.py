from __future__ import annotations

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


def repository_identity_root(edit_path: Path) -> Path:
    """Return the Git repository toplevel enclosing ``edit_path`` (REQ-REPO-005).

    Nested Git repositories, submodules, linked worktrees, and external repository
    mounts each resolve to their own distinct identity root. The result is fully
    resolved so that junctions/symlinks collapse to the repository they point at.
    """
    path = edit_path.expanduser().resolve()
    cwd = path if path.is_dir() else path.parent
    if not cwd.is_dir():
        raise WorkspaceAffinityError(
            f"Edit path {edit_path} is not inside a reachable directory"
        )
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=30)
    if result.returncode != 0:
        raise WorkspaceAffinityError(
            f"Unable to resolve repository identity root for {edit_path}: {result.stdout.strip()}"
        )
    raw = result.stdout.strip().splitlines()[0]
    if not raw:
        raise WorkspaceAffinityError(
            f"Git returned an empty repository toplevel for {edit_path}"
        )
    return Path(raw).resolve()


def assert_edit_repository_authority(
    project: dict, edit_path: Path, primary_repo: Path
) -> Path:
    """Fail closed unless ``project`` explicitly authorizes editing ``edit_path``'s repo.

    Ordinary content inside ``primary_repo`` is admitted implicitly. Separate-identity
    repositories (nested repos, submodules, linked worktrees, external mounts) are
    rejected unless their resolved root appears in ``project["authorized_repository_roots"]``.
    """
    identity_root = repository_identity_root(edit_path)
    primary = primary_repo.expanduser().resolve()
    if identity_root == primary:
        return identity_root

    authorized = project.get("authorized_repository_roots") or {}
    if authorized.get(str(identity_root)):
        return identity_root

    raise WorkspaceAffinityError(
        f"Project {project.get('id', '<unknown>')} is not authorized to edit repository "
        f"{identity_root}. Add {identity_root} to authorized_repository_roots or route the "
        "edit to its own project."
    )
