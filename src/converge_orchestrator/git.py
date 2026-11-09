from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .shell import run
from .workspace_ownership import (
    WorkspaceOwnershipError,
    WorkspaceOwnershipStore,
)


class GitError(RuntimeError):
    pass


def _reject_boundary_escape(
    path: Path,
    authorized_root: Path,
    context: str,
) -> Path:
    """Resolve *path* and reject traversal, symlink, junction, mount, or alias escapes."""
    path = path.expanduser()
    authorized_root = authorized_root.expanduser().resolve()
    logical = path.parent.resolve() / path.name
    resolved = path.resolve()
    if resolved != logical:
        raise GitError(
            f"REQ-REPO-003 boundary escape rejected: {context} alias ({path} -> {resolved})"
        )
    if resolved != authorized_root and authorized_root not in resolved.parents:
        raise GitError(
            f"REQ-REPO-003 boundary escape rejected: {context} outside root: {resolved}"
        )
    return resolved


def _git(repo: Path, *args: str, timeout: int = 300) -> str:
    result = run(["git", *args], cwd=repo, timeout=timeout)
    if result.returncode != 0:
        raise GitError(result.stdout)
    return result.stdout.strip()


def _git_stdout(repo: Path, *args: str, timeout: int = 300) -> str:
    """Run a git command and return only its stdout.

    Unlike `_git`, this captures stdout and stderr separately so that git's
    diagnostic output (CRLF/EOL warnings, etc.) never contaminates the
    semantic candidate diff used for fingerprinting.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        encoding="utf-8",
        errors="replace",
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"{result.stdout}{result.stderr}".strip())
    return result.stdout


def _git_lines(repo: Path, *args: str, timeout: int = 300) -> list[str]:
    """Run a path-listing git command and return only its stdout lines.

    run() merges stderr into stdout, so git's CRLF/EOL warnings (emitted on stderr while a
    core.autocrlf working copy is read) would otherwise be parsed as file paths. _git() also
    strips the whole command output, which removes the first porcelain line's leading status
    column and shifts any positional column parse by one character; a leading-dot path such as
    .github/... was then enumerated as github/... (external acceptance V6 defect). Path
    enumeration must read stdout with stderr captured separately so diagnostics never become
    file paths and no output edge can shift the parse.
    """

    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        encoding="utf-8",
        errors="replace",
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"{result.stdout}{result.stderr}".strip())
    return [line for line in result.stdout.splitlines() if line]


def _origin_remote_exists(repo: Path) -> bool:
    """Return True when an ``origin`` remote is configured in ``repo``.

    This is a non-network probe: it only reads local Git configuration.
    """
    result = run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        timeout=60,
    )
    return result.returncode == 0


def resolve_base_ref(repo: Path, base_branch: str) -> str:
    """Return the full ref to use as the convergence base.

    Prefer the remote tracking ref ``refs/remotes/origin/{base_branch}`` when
    an ``origin`` remote is configured so origin-connected repositories keep
    their existing fetch/pull semantics.  Fall back to the local branch ref
    ``refs/heads/{base_branch}`` for local-only repositories that have commits
    but no ``origin`` remote.
    """
    if _origin_remote_exists(repo):
        return f"refs/remotes/origin/{base_branch}"
    return f"refs/heads/{base_branch}"


def resolve_canonical_base_revision(repo: Path, base_branch: str) -> str | None:
    """Return the commit SHA of the canonical base ref, or None when absent.

    Mirrors ``resolve_base_ref`` semantics and returns ``None`` for an unborn
    branch or any git-unavailability condition, so callers record the base
    revision exactly when one exists.
    """
    try:
        base_ref = resolve_base_ref(repo, base_branch)
        result = subprocess.run(
            ["git", "rev-parse", "--verify", base_ref],
            cwd=repo,
            encoding="utf-8",
            errors="replace",
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Missing repository, unavailable git, or hung git command: treat as no base.
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def ensure_clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain"):
        raise GitError("Repository must be clean before orchestration starts.")


def current_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def update_base(repo: Path, branch: str) -> str:
    if _origin_remote_exists(repo):
        _git(repo, "fetch", "origin")
        _git(repo, "checkout", branch)
        _git(repo, "pull", "--ff-only", "origin", branch)
    else:
        _git(repo, "checkout", branch)
    return current_head(repo)


def _worktree_entries(repo: Path) -> dict[Path, str | None]:
    output = _git(repo, "worktree", "list", "--porcelain")
    entries: dict[Path, str | None] = {}
    target: Path | None = None
    branch: str | None = None

    def flush() -> None:
        nonlocal target, branch
        if target is not None:
            entries[target] = branch
        target = None
        branch = None

    for line in [*output.splitlines(), ""]:
        if not line:
            flush()
            continue
        if line.startswith("worktree "):
            flush()
            target = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
    return entries


def worktree_entries(repo: Path) -> dict[Path, str | None]:
    """Return Git-registered worktree paths and their local branches."""
    return _worktree_entries(repo)


def _local_branch_exists(repo: Path, branch: str) -> bool:
    result = run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        timeout=60,
    )
    if result.returncode not in (0, 1):
        raise GitError(result.stdout)
    return result.returncode == 0


def _validate_cleanup_registration(repo: Path, target: Path, branch: str) -> None:
    target = target.expanduser().resolve()
    entries = _worktree_entries(repo)
    registered_branch = entries.get(target)
    if target in entries and registered_branch != branch:
        raise GitError(
            f"Refusing cleanup: worktree {target} belongs to {registered_branch}, "
            f"expected {branch}"
        )
    for registered_path, registered in entries.items():
        if registered == branch and registered_path != target:
            raise GitError(
                f"Refusing cleanup: branch {branch} belongs to {registered_path}, "
                f"not {target}"
            )
    if target.exists() and target not in entries:
        raise GitError(
            f"Refusing cleanup of unregistered filesystem path: {target}"
        )


def _remove_registered_worktree(repo: Path, target: Path, branch: str) -> None:
    target = target.expanduser().resolve()
    entries = _worktree_entries(repo)
    if target in entries:
        _git(repo, "worktree", "remove", "--force", str(target))
    if target.exists():
        raise GitError(f"Unable to clean stale worktree: {target}")
    result = run(["git", "branch", "-D", branch], cwd=repo, timeout=60)
    if result.returncode not in (0, 1):
        raise GitError(result.stdout)


def cleanup_worktree(
    repo: Path,
    target: Path,
    branch: str,
    *,
    reason: str = "controlled_cleanup",
) -> None:
    """Remove a worktree only after persisting explicit cleanup intent."""
    authorized_root = next(
        (p.parent for p, b in _worktree_entries(repo).items() if b == branch), target.parent
    )
    _reject_boundary_escape(target, authorized_root, "cleanup target")
    target = target.expanduser().resolve()
    store = WorkspaceOwnershipStore(target.parent)
    store.request_cleanup(target=target, branch=branch, reason=reason)
    _validate_cleanup_registration(repo, target, branch)
    _remove_registered_worktree(repo, target, branch)
    store.mark_released(target=target, branch=branch)


def garbage_collect_requested_worktrees(
    repo: Path,
    worktree_root: Path,
    branch_prefix: str = "converge/",
    *,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Finish only cleanup operations that were explicitly requested before a crash."""
    root = worktree_root.expanduser().resolve()
    store = WorkspaceOwnershipStore(root)
    results: list[dict[str, Any]] = []
    try:
        records = store.list_records()
    except WorkspaceOwnershipError as exc:
        return [{"status": "blocked", "reason": str(exc)}]

    for record in records:
        if record.status != "cleanup_requested":
            continue
        target = Path(record.path).expanduser().resolve()
        item: dict[str, Any] = {
            "task_id": record.task_id,
            "path": str(target),
            "branch": record.branch,
        }
        if not record.branch.startswith(branch_prefix):
            item.update(
                status="blocked",
                reason=(
                    f"owned branch does not match configured prefix {branch_prefix}: "
                    f"{record.branch}"
                ),
            )
            results.append(item)
            continue
        try:
            _validate_cleanup_registration(repo, target, record.branch)
            if dry_run:
                item["status"] = "would_release"
            else:
                _remove_registered_worktree(repo, target, record.branch)
                store.mark_released(target=target, branch=record.branch)
                item["status"] = "released"
        except (GitError, WorkspaceOwnershipError) as exc:
            item.update(status="blocked", reason=str(exc))
        results.append(item)
    return results


def create_worktree(
    repo: Path,
    worktree_root: Path,
    task_id: str,
    base_branch: str,
    branch_prefix: str = "converge/",
) -> tuple[Path, str]:
    """Create or safely adopt the deterministic worktree for a LangGraph task.

    LangGraph checkpoints provide at-least-once node execution after a process crash. This function
    therefore never destroys an existing candidate as part of creation. A matching registered
    worktree is adopted, a preserved local task branch is reattached, and any ambiguous filesystem
    state fails closed for explicit recovery instead of being force-deleted.
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", task_id).strip("-").lower()
    branch = f"{branch_prefix}{safe}"
    _reject_boundary_escape(worktree_root, worktree_root.parent, "worktree_root")
    root = worktree_root.expanduser().resolve()
    target = (root / safe).resolve()
    if target.parent != root:
        raise GitError(f"Unsafe worktree target outside configured root: {target}")

    store = WorkspaceOwnershipStore(root)
    existing_owner = store.read(branch)
    if existing_owner is not None:
        if Path(existing_owner.path).resolve() != target:
            raise GitError(
                f"Workspace ownership for {branch} points to {existing_owner.path}, "
                f"expected {target}"
            )
        if existing_owner.status == "cleanup_requested":
            raise GitError(
                f"Workspace {branch} has pending cleanup; finish GC before reactivation"
            )

    entries = _worktree_entries(repo)
    registered_branch = entries.get(target)
    if target.exists() or target in entries:
        if registered_branch is None:
            raise GitError(
                f"Existing worktree path is not a registered branch worktree: {target}"
            )
        if registered_branch != branch:
            raise GitError(
                f"Existing worktree {target} belongs to {registered_branch}, expected {branch}"
            )
        if not target.exists():
            raise GitError(f"Git registers worktree but its path is missing: {target}")
        try:
            store.activate(task_id=task_id, target=target, branch=branch)
        except WorkspaceOwnershipError as exc:
            raise GitError(str(exc)) from exc
        return target, branch

    root.mkdir(parents=True, exist_ok=True)
    if _local_branch_exists(repo, branch):
        _git(repo, "worktree", "add", str(target), branch)
    else:
        base_ref = resolve_base_ref(repo, base_branch)
        _git(repo, "worktree", "add", "-b", branch, str(target), base_ref)
    try:
        store.activate(task_id=task_id, target=target, branch=branch)
    except WorkspaceOwnershipError as exc:
        raise GitError(str(exc)) from exc
    return target, branch


def _untracked_files_diff(worktree: Path) -> str:
    """Return a deterministic diff-like representation of untracked candidate files.

    Untracked files are invisible to ``git diff origin/base...HEAD`` and ``git diff HEAD``,
    so they must be represented explicitly so every review lane inspects a complete
    publishable candidate snapshot. Regenerable cache artifacts are excluded.
    """

    hunks: list[str] = []
    for path in sorted(_git_lines(worktree, "ls-files", "--others", "--exclude-standard")):
        if is_deterministic_cache_artifact(path):
            continue
        candidate = worktree / path
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = candidate.read_bytes().decode("utf-8", errors="replace")
        lines = content.splitlines()
        body = "\n".join(f"+{line}" for line in lines)
        hunks.append(
            "\n".join(
                [
                    f"diff --git a/{path} b/{path}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{path}",
                    f"@@ -0,0 +1,{len(lines)} @@",
                    body,
                ]
            )
        )
    return "\n".join(hunks)


def diff(worktree: Path, base_branch: str) -> str:
    base_ref = resolve_base_ref(worktree, base_branch)
    committed = _git_stdout(worktree, "diff", f"{base_ref}...HEAD")
    working = _git_stdout(worktree, "diff", "HEAD")
    untracked = _untracked_files_diff(worktree)
    return "\n".join(part for part in (committed, working, untracked) if part).strip()


_CACHE_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_CACHE_FILE_SUFFIXES = (".pyc", ".pyo")


def is_deterministic_cache_artifact(path: str) -> bool:
    """Match regenerable interpreter/test-runner artifacts produced by gate execution itself.

    Bytecode caches and test-runner caches are deterministic byproducts of running the quality
    gates inside the candidate worktree, not candidate changes. Treating them as worktree
    mutations would make every gate that runs a test interpreter invalidate its own evidence.
    """

    posix = PurePosixPath(path.replace("\\", "/"))
    if posix.suffix in _CACHE_FILE_SUFFIXES:
        return True
    return any(part in _CACHE_DIRECTORY_NAMES for part in posix.parts)


def changed_files(worktree: Path, base_branch: str) -> list[str]:
    """List candidate-changed files by content, immune to EOL/stat status noise.

    Tracked changes come from content-based `git diff --name-only`, so files the sandbox
    rewrote byte-identically with a different EOL convention (a core.autocrlf smudge
    mismatch: `git status` reports them modified while the content diff is empty) never
    count as candidate changes. Untracked files are enumerated individually with
    `ls-files --others --exclude-standard` -- the same per-file guarantee as
    `status --porcelain -uall`, so per-file cache-artifact filtering stays exact.
    """

    base_ref = resolve_base_ref(worktree, base_branch)
    names: set[str] = set()
    for args in (
        ("diff", "--name-only", f"{base_ref}...HEAD"),
        ("diff", "--name-only", "HEAD"),
    ):
        names.update(_git_lines(worktree, *args))
    names.update(_git_lines(worktree, "ls-files", "--others", "--exclude-standard"))
    return sorted(
        name for name in names if not is_deterministic_cache_artifact(name)
    )


def diff_line_count(worktree: Path, base_branch: str) -> int:
    total = 0
    tracked: set[str] = set()
    base_ref = resolve_base_ref(worktree, base_branch)
    for line in _git_lines(worktree, "diff", "--numstat", base_ref):
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        tracked.add(path)
        if added.isdigit():
            total += int(added)
        if deleted.isdigit():
            total += int(deleted)
    for path in _git_lines(worktree, "ls-files", "--others", "--exclude-standard"):
        if path in tracked or is_deterministic_cache_artifact(path):
            continue
        candidate = worktree / path
        if candidate.is_file():
            try:
                total += len(candidate.read_text(encoding="utf-8").splitlines())
            except UnicodeDecodeError:
                total += 1
    return total


def paths_within_allowlist(paths: list[str], patterns: list[str]) -> bool:
    if not patterns:
        return True
    return all(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in paths)


def delete_remote_branch(repo: Path, branch: str) -> None:
    run(["git", "push", "origin", "--delete", branch], cwd=repo, timeout=300)


def _remove_untracked_cache_artifacts(worktree: Path) -> None:
    """Delete only untracked regenerable cache artifacts so they never enter candidate commits."""

    _reject_boundary_escape(worktree, worktree.parent, "worktree")
    root = worktree.resolve()
    prunable: set[Path] = set()
    status = _git(worktree, "status", "--porcelain", "-uall")
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not path or not is_deterministic_cache_artifact(path):
            continue
        target = (worktree / path).resolve()
        if root not in target.parents:
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
            prunable.add(target.parent)
        elif target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
            prunable.add(target.parent)
    for parent in sorted(prunable, key=lambda item: len(item.parts), reverse=True):
        _prune_empty_cache_ancestors(root, parent)


def _prune_empty_cache_ancestors(root: Path, start: Path) -> None:
    """Remove cache-named directories left empty by artifact deletion, up to the worktree root."""

    current = start.resolve()
    while root in current.parents:
        if not current.is_dir() or any(current.iterdir()):
            return
        relative_parts = current.relative_to(root).parts
        if current.name not in _CACHE_DIRECTORY_NAMES and relative_parts[0] not in (
            _CACHE_DIRECTORY_NAMES
        ):
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _git_nul_fields(worktree: Path, *args: str, timeout: int = 300) -> list[bytes]:
    """Run a git ``-z`` command and return its non-empty NUL-separated fields as bytes.

    ``-z`` output is never C-quoted and is not line-oriented, so it remains correct for
    pathnames that ``core.quotepath`` would otherwise quote.  Callers decode the bytes
    with the filesystem encoding so the resulting strings match the worktree paths.
    """

    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise GitError(f"{stdout}{stderr}".strip())
    if not result.stdout:
        return []
    return [field for field in result.stdout.split(b"\x00") if field]


def secondary_repository_roots(worktree: Path) -> list[str]:
    """Return sorted relative paths to secondary repository roots inside *worktree*.

    Uses only local Git metadata:
      - Staged gitlinks (index mode 160000) from ``git ls-files -s -z``.
      - Untracked paths whose directory chain contains a ``.git`` entry.

    Any present ``.git`` entry -- whether a directory, a linked-worktree ``gitdir``
    pointer file, or a symlinked external repository mount -- is treated as a secondary
    repository root.  This is an intentional fail-closed interpretation: authority over
    the primary repository does not imply authority over a nested repository identity.
    Because a tracked submodule's root path equals the gitlink, a content-dirty submodule
    worktree (e.g. `` m sub``) is also reported as a root mutation and rejected.

    No network, model, provider, or fetch operations.
    """

    roots: set[str] = set()
    for field in _git_nul_fields(worktree, "ls-files", "-s", "-z"):
        if b"\t" not in field:
            continue
        metadata, path = field.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode == b"160000":
            roots.add(PurePosixPath(os.fsdecode(path).replace("\\", "/")).as_posix())
    for field in _git_nul_fields(worktree, "status", "--porcelain", "-z", "-uall"):
        if len(field) < 3 or field[:3] != b"?? ":
            continue
        raw = os.fsdecode(field[3:])
        candidate = worktree / raw
        # The untracked entry itself may be the secondary repository root.
        if (candidate / ".git").exists():
            roots.add(PurePosixPath(raw.replace("\\", "/")).as_posix())
            continue
        # Or it may be a file/directory inside an embedded repository.
        for parent in candidate.parents:
            if parent == worktree:
                break
            if (parent / ".git").exists():
                roots.add(parent.relative_to(worktree).as_posix())
                break
    return sorted(roots)


_STATUS_PAIR_CHARS = frozenset(b" MADRCUT?!mu")


def _posix_path_from_nul_field(field: bytes) -> str:
    return PurePosixPath(os.fsdecode(field).replace("\\", "/")).as_posix()


def _candidate_mutations(worktree: Path) -> set[str]:
    """Return relative paths of all candidate mutations in *worktree* before staging.

    Combines already-staged changes, tracked unstaged changes, untracked files, and
    submodule/dirty-directory status entries.  Paths are read via ``-z`` so they remain
    correct even when ``core.quotepath`` would otherwise C-quote them.

    ``status --porcelain -z`` entries are validated as two-letter porcelain status
    pairs before slicing, and both fields of a rename/copy pair are retained so that
    the destination (and source) can be matched against secondary repository roots
    regardless of field ordering.
    """

    names: set[str] = set()
    for args in (
        ("diff", "--cached", "--name-only", "-z", "HEAD"),
        ("diff", "--name-only", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        names.update(
            _posix_path_from_nul_field(field)
            for field in _git_nul_fields(worktree, *args)
        )
    fields = _git_nul_fields(worktree, "status", "--porcelain", "-z", "-uall")
    i = 0
    while i < len(fields):
        field = fields[i]
        if (
            len(field) < 3
            or field[2:3] != b" "
            or field[0] not in _STATUS_PAIR_CHARS
            or field[1] not in _STATUS_PAIR_CHARS
        ):
            i += 1
            continue
        names.add(_posix_path_from_nul_field(field[3:]))
        if b"R" in field[:2] or b"C" in field[:2]:
            if i + 1 < len(fields):
                names.add(_posix_path_from_nul_field(fields[i + 1]))
            i += 2
            continue
        i += 1
    return names


def _staged_path_set(worktree: Path) -> set[str]:
    """Return the set of staged relative paths."""
    return {
        _posix_path_from_nul_field(field)
        for field in _git_nul_fields(worktree, "diff", "--cached", "--name-only", "-z", "HEAD")
    }


def _staged_gitlink_paths(worktree: Path) -> list[str]:
    """Return relative paths of all gitlinks (mode 160000) present in the index.

    Callers that need only gitlinks newly staged relative to HEAD must intersect the
    result with the set of paths reported by ``git diff --cached --name-only HEAD``.
    """
    paths: list[str] = []
    for field in _git_nul_fields(worktree, "ls-files", "-s", "-z"):
        if b"\t" not in field:
            continue
        metadata, path = field.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode == b"160000":
            paths.append(_posix_path_from_nul_field(path))
    return paths


def _unstage_roots_safely(worktree: Path, roots: set[str]) -> None:
    """Reset staged entries under *roots* without letting pathspec magic select other paths.

    Errors are ignored: the caller raises the deterministic REQ-REPO-005 GitError next,
    so no primary-repository commit can be created regardless of reset outcome.
    """
    if not roots:
        return
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    try:
        subprocess.run(
            ["git", "reset", "--", *sorted(roots)],
            cwd=worktree,
            env=env,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except Exception:
        # The caller's unconditional marker GitError is the observable contract.
        pass


def _reject_secondary_repository_mutations(worktree: Path) -> None:
    """Fail closed when the candidate would mutate a secondary repository identity.

    A secondary repository identity is either a staged gitlink (index mode 160000) or an
    embedded repository whose ``.git`` metadata is detected under an untracked path. Any
    candidate mutation under such a root is rejected before staging so no primary-repository
    commit is created and the nested repository remains untouched.  Any staged index entry
    for the offending root is reset before raising so the gitlink does not remain staged.

    Only roots that currently hold staged index entries are reset, repository-derived
    pathspecs are neutralized via ``GIT_LITERAL_PATHSPECS=1``, and the marker GitError is
    raised unconditionally even if a reset subprocess fails, so the deterministic message
    cannot be replaced by an incidental git diagnostic.
    """

    roots = secondary_repository_roots(worktree)
    if not roots:
        return
    root_paths = [PurePosixPath(root.replace("\\", "/")) for root in roots]
    staged = _staged_path_set(worktree)
    offending: set[str] = set()
    for path in _candidate_mutations(worktree):
        if is_deterministic_cache_artifact(path):
            continue
        posix = PurePosixPath(path.replace("\\", "/"))
        for root in root_paths:
            if posix == root or root in posix.parents:
                offending.add(root.as_posix())
    if not offending:
        return
    staged_roots = {
        root.as_posix()
        for root in root_paths
        if any(
            staged_path == root.as_posix()
            or staged_path.startswith(root.as_posix() + "/")
            for staged_path in staged
        )
    }
    _unstage_roots_safely(worktree, staged_roots)
    raise GitError("REQ-REPO-005 secondary repository mutation rejected")


def commit_all(worktree: Path, message: str) -> str | None:
    """Commit every candidate change, skipping noise-only candidate states.

    Gate execution leaves untracked cache artifacts and EOL-phantom status entries in the
    candidate worktree (external acceptance V15, run 88c5862fecc04ee0be6db1ad5b466396). The
    pre-cleanup status check passed on that noise, the cache cleanup then removed it, and the
    unconditional commit ran with an index that matched HEAD, so git exited 1 with "nothing to
    commit" and the raw GitError aborted the run before the designed no_changes terminal was
    reachable. Detection must therefore be staged-diff semantics, never the pre-staging status:
    after cleanup and staging, commit only when `git diff --cached` (parsed stdout-only so CRLF
    warnings never become entries) reports a real staged diff relative to HEAD; otherwise return
    None so integrate reaches its designed no_changes outcome. Genuine git errors still fail
    closed via GitError.

    Nested repository identities remain separate (REQ-REPO-005): any candidate mutation that
    touches a staged gitlink or a path under a detected secondary repository root is rejected
    before staging so neither the primary nor the nested repository is committed implicitly.
    Defense in depth also scans the staged index after ``git add -A`` and resets any
    escaped gitlink that is newly staged relative to HEAD before raising the same
    GitError.  A tracked submodule with a dirty worktree is rejected too, because its
    root path is reported as a candidate mutation.  This is the acknowledged fail-closed
    policy; narrowing the scope is reserved for a follow-up increment.

    All inventory and reset commands used by the REQ-REPO-005 guard remain local-only:
    no network, model, provider, or fetch operations.
    """
    _remove_untracked_cache_artifacts(worktree)
    _reject_secondary_repository_mutations(worktree)
    _git(worktree, "add", "-A")
    staged_gitlinks = set(_staged_gitlink_paths(worktree)) & _staged_path_set(worktree)
    if staged_gitlinks:
        _unstage_roots_safely(worktree, staged_gitlinks)
        raise GitError("REQ-REPO-005 secondary repository mutation rejected")
    staged = _git_lines(worktree, "diff", "--cached", "--name-only", "HEAD")
    if not staged:
        return None
    _git(worktree, "commit", "-m", message)
    return current_head(worktree)


def existing_candidate_commit(worktree: Path, base_branch: str) -> str | None:
    """Return an already-created candidate commit after a checkpoint race/crash."""
    if _git(worktree, "status", "--porcelain"):
        return None
    base_ref = resolve_base_ref(worktree, base_branch)
    ahead = _git(worktree, "rev-list", "--count", f"{base_ref}..HEAD")
    try:
        count = int(ahead)
    except ValueError as exc:
        raise GitError(f"Invalid git rev-list count: {ahead}") from exc
    return current_head(worktree) if count > 0 else None


def push(worktree: Path, branch: str) -> None:
    _git(worktree, "push", "-u", "origin", branch, timeout=900)


def _source_policy_bound(source: object) -> bool:
    """True when a source record is bound by project policy (mapping or attribute form)."""
    if isinstance(source, Mapping):
        value: object = source.get("policy_bound", False)
    else:
        value = getattr(source, "policy_bound", False)
    return bool(value)


class GitObjectAvailability:
    """Pure, deterministic, in-memory Git object availability predicate (INV-GITOBJ-001).

    Candidate readiness, accepted integration, and terminal convergence must not depend
    on a Git object available only through an undeclared local alternate, stale promisor
    storage, incomplete shallow history, or another repository-object source not bound by
    project policy. Given the required object ids and the sources each is reachable
    through, ``ready`` is True only when every required object is reachable through at
    least one policy-bound source (an unbound source never admits an object, but a bound
    one does even when unbound sources also hold it), and ``unbound`` is the sorted tuple
    of exactly the required object ids that have no policy-bound source -- including
    objects absent from ``sources``. Fails closed for any such object.

    No network, model, provider, subprocess, git command, or filesystem I/O.
    """

    def __init__(
        self,
        required: Sequence[str],
        sources: Mapping[str, Sequence[object]],
    ) -> None:
        def _is_unbound(object_id: str) -> bool:
            return not any(
                _source_policy_bound(source) for source in sources.get(object_id, ())
            )

        self.ready = all(not _is_unbound(object_id) for object_id in required)
        self.unbound = tuple(
            sorted({object_id for object_id in required if _is_unbound(object_id)})
        )


def _source_is_ref_independent(source: object) -> bool:
    """True when a source record is a non-mutable, ref-independent retention anchor.

    Fails closed: a source that does not explicitly declare ``mutable`` as falsy is not
    treated as a ref-independent anchor, so a retained release cannot rely on it.
    """
    if isinstance(source, Mapping):
        declared = "mutable" in source
        mutable: object = source.get("mutable")
    else:
        declared = hasattr(source, "mutable")
        mutable = getattr(source, "mutable", None)
    return declared and not bool(mutable)


class RetainedReleaseAuditability:
    """Pure, deterministic, in-memory retained-release auditability predicate (INV-RELEASE-001).

    A retained historical release SHALL remain independently auditable after later branch
    movement, force-update, ref deletion, repository maintenance, or ordinary remote garbage
    collection and SHALL NOT depend on the released commit remaining reachable from a mutable
    Git ref. Given the released commit, the retained audit element ids, the retention sources
    each element is anchored by, and the ref events that have since occurred, ``auditable`` is
    True only when every retained audit element is anchored by at least one non-mutable,
    ref-independent retention source (a mutable Git ref never admits an element, but a
    ref-independent source does even when a mutable ref also holds it), and ``ref_dependent``
    is the sorted tuple of exactly the retained audit element ids that have no ref-independent
    anchor -- including elements absent from ``retention_sources``. Fails closed for any such
    element. ``ref_events`` records later ref activity and never changes the decision, because
    the decision depends only on ref-independent retention anchors, not on mutable ref
    reachability.

    No network, model, provider, subprocess, git command, or filesystem I/O.
    """

    def __init__(
        self,
        released_commit: str,
        retained_audits: Sequence[str],
        retention_sources: Mapping[str, Sequence[object]],
        ref_events: Sequence[object] = (),
    ) -> None:
        def _is_ref_dependent(element_id: str) -> bool:
            return not any(
                _source_is_ref_independent(source)
                for source in retention_sources.get(element_id, ())
            )

        self.auditable = all(
            not _is_ref_dependent(element_id) for element_id in retained_audits
        )
        self.ref_dependent = tuple(
            sorted(
                {
                    element_id
                    for element_id in retained_audits
                    if _is_ref_dependent(element_id)
                }
            )
        )
