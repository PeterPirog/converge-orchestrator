from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from .shell import run

_WORKSPACE_MARKER_NAME = "converge-workspace-id"
_STATE_MARKER_NAME = ".converge-state-id"


def _git_env() -> dict[str, str]:
    """Return the current process environment with GIT_* variables removed."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class WorkspaceAffinityError(RuntimeError):
    pass


def _git_common_dir(repo: Path, env: dict[str, str] | None = None) -> Path:
    result = run(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, timeout=30, env=env,
        inherit_env=env is None,
    )
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


def _git_toplevel(repo: Path, env: dict[str, str] | None = None) -> Path:
    result = run(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=30, env=env,
        inherit_env=env is None,
    )
    if result.returncode != 0:
        raise WorkspaceAffinityError(
            f"Unable to resolve Git worktree root for {repo}: {result.stdout.strip()}"
        )
    raw = result.stdout.strip()
    if not raw:
        raise WorkspaceAffinityError(f"Git returned an empty worktree root for {repo}")
    return Path(raw).expanduser().resolve()


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


def _is_within(candidate: Path, root: Path) -> bool:
    """Return True when ``candidate`` is ``root`` or a descendant of ``root``.

    Normalization is symlink-agnostic so a junction or symlink under ``root`` is still
    considered under ``root``.
    """
    root_str = os.path.normcase(os.path.normpath(os.path.abspath(str(root))))
    cand_str = os.path.normcase(os.path.normpath(os.path.abspath(str(candidate))))
    return cand_str == root_str or cand_str.startswith(root_str + os.sep)


def _nearest_existing_ancestor(path: Path) -> Path:
    """Return the nearest directory ancestor of ``path`` that exists on disk.

    Walks upward until an existing directory is found.  Raises ``WorkspaceAffinityError``
    if no directory ancestor exists.
    """
    candidate = path
    while (not candidate.exists() or not candidate.is_dir()) and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir():
        raise WorkspaceAffinityError(
            f"Unable to locate an existing directory ancestor for {path}; "
            "explicit workspace binding is required."
        )
    return candidate


def _has_git_entry(path: Path) -> bool:
    """Return True when ``path`` contains a ``.git`` directory or gitdir link.

    An inaccessible .git entry is treated as present so classification stays
    fail-closed even when ACLs hide it from a plain exists() check.
    """
    git_entry = path / ".git"
    try:
        os.lstat(str(git_entry))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _ambiguous_git_ancestor(cwd: Path, primary: Path) -> bool:
    """Return True if an ancestor between ``cwd`` and ``primary`` has a ``.git`` entry."""
    candidate = cwd
    while candidate != primary and _is_within(candidate, primary):
        if _has_git_entry(candidate):
            return True
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return False


def resolve_separate_repository_identity(primary_root: Path, target: Path) -> Path | None:
    """Classify a working path under ``primary_root`` as primary-owned or separate identity.

    Returns the resolved worktree root of a separate nested repository, submodule, foreign linked
    worktree, or external mount reachable from ``primary_root``; returns ``None`` for paths that
    belong to the primary identity.

    A path that is neither lexically inside ``primary_root`` nor physically inside the resolved
    primary tree raises ``WorkspaceAffinityError``.  An in-tree junction or symlink that resolves
    to a separate reachable Git identity is classified as that separate identity and requires an
    explicit workspace binding for it.  Targets whose Git identity is ambiguous (a ``.git`` entry
    is present in the ancestor chain but its metadata is unreachable) fail closed so primary
    authority is never silently implied.  A linked worktree of the same clone shares the primary
    identity and is therefore classified as primary-owned.
    """
    # Authoritative classification requires an absolute target. Resolve a relative
    # target against the primary root rather than the controller process CWD.
    if not target.is_absolute():
        target = primary_root.expanduser() / target

    primary = primary_root.expanduser().resolve()

    try:
        resolved_target = target.expanduser().resolve()
    except OSError as exc:
        raise WorkspaceAffinityError(
            f"Unable to resolve target {target} for repository classification"
        ) from exc

    # Reject targets that are neither lexically nor physically inside the primary tree.
    requested = Path(os.path.normpath(os.path.abspath(str(target.expanduser()))))
    if not _is_within(requested, primary) and not _is_within(resolved_target, primary):
        raise WorkspaceAffinityError(f"Target {target} is outside the primary root {primary_root}")

    # Resolve the primary Git common directory; missing Git is a fail-closed error.
    try:
        primary_common = _git_common_dir(primary, env=_git_env())
    except (WorkspaceAffinityError, OSError) as exc:
        raise WorkspaceAffinityError(
            f"Primary root {primary_root} is not a reachable Git workspace"
        ) from exc

    try:
        primary_toplevel = _git_toplevel(primary, env=_git_env())
    except (WorkspaceAffinityError, OSError):
        primary_toplevel = None

    # Probe from an existing directory.  If the exact target (or its parent) does not exist,
    # walk up to the nearest existing ancestor.  This prevents a missing subpath inside a
    # separate identity from being misclassified as primary territory.
    if resolved_target.exists():
        cwd = resolved_target if resolved_target.is_dir() else resolved_target.parent
    else:
        cwd = _nearest_existing_ancestor(resolved_target.parent)

    try:
        target_common = _git_common_dir(cwd, env=_git_env())
    except (WorkspaceAffinityError, OSError) as exc:
        # A .git entry in the ancestor chain, but unreachable Git metadata, signals an
        # ambiguous separate identity.  Do not fall back to primary ownership.
        if _ambiguous_git_ancestor(cwd, primary):
            raise WorkspaceAffinityError(
                f"Target {target} lies under a Git entry that cannot be resolved; "
                "explicit workspace binding for the separate identity is required."
            ) from exc
        # No Git repository reachable; primary territory only if the exact primary
        # toplevel confirms ownership.  A relative or non-toplevel primary root is
        # not enough to imply authority.
        if primary_toplevel is not None and _is_within(cwd, primary_toplevel):
            return None
        raise WorkspaceAffinityError(
            f"Target {target} resolves outside the primary worktree and is not a reachable "
            "Git workspace; explicit workspace binding is required."
        ) from exc

    try:
        target_toplevel = _git_toplevel(cwd, env=_git_env())
    except (WorkspaceAffinityError, OSError) as exc:
        # Git metadata reachable but not a worktree (bare repo or separate .git metadata).
        raise WorkspaceAffinityError(
            f"Target {target} resolves to Git metadata outside the primary worktree; "
            "explicit workspace binding for the separate identity is required."
        ) from exc

    if target_common == primary_common:
        if not _is_within(resolved_target, primary):
            raise WorkspaceAffinityError(
                f"Target {target} resolves outside the primary worktree but shares its "
                "Git identity; explicit workspace binding is required."
            )
        return None
    return target_toplevel


def assert_repository_identity_authority(
    project: dict, primary_root: Path, target: Path
) -> Path | None:
    """Reject execution unless the project is explicitly bound to the identity that owns ``target``.

    Returns ``None`` for primary-owned paths after checking ``primary_root``; returns the
    separate identity root when the project is bound to that identity.  Probing authority may
    materialize a clone-local Converge workspace-id marker in the separate identity's Git
    metadata; the marker is untracked.
    """
    separate = resolve_separate_repository_identity(primary_root, target)
    if separate is None:
        assert_workspace_affinity(project, primary_root)
        return None
    assert_workspace_affinity(project, separate)
    return separate
