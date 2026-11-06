from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    identity: tuple[str, str] | None
    base_branch_exists: bool
    remote_configured: bool
    remote_url: str | None
    clean: bool


_OVERLAP_PAIRS = [
    ("state_path", "repository_path"),
    ("source_of_truth_path", "repository_path"),
    ("secret_path", "repository_path"),
    ("controller_authority_path", "repository_path"),
    ("source_of_truth_path", "state_path"),
    ("secret_path", "state_path"),
    ("controller_authority_path", "state_path"),
    ("secret_path", "source_of_truth_path"),
    ("controller_authority_path", "source_of_truth_path"),
    ("controller_authority_path", "secret_path"),
]


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            encoding="utf-8",
            errors="replace",
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        argv = " ".join(exc.cmd)
        raise PreflightError(f"git command timed out after 60s: {argv}") from None


def _git_config(repo: Path, key: str) -> str | None:
    result = _run_git(repo, "config", "--local", key)
    if result.returncode not in (0, 1):
        raise PreflightError(f"git config failed: {result.stderr.strip()}")
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _remote_url(repo: Path) -> str | None:
    result = _run_git(repo, "remote")
    if result.returncode != 0:
        raise PreflightError(f"git remote failed: {result.stderr.strip()}")
    names = result.stdout.strip().splitlines()
    if not names:
        return None
    url = _run_git(repo, "remote", "get-url", names[0])
    if url.returncode not in (0, 1):
        raise PreflightError(f"git remote get-url failed: {url.stderr.strip()}")
    return url.stdout.strip() if url.returncode == 0 else None


def _local_branch_exists(repo: Path, branch: str) -> bool:
    result = _run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise PreflightError(f"git show-ref failed: {result.stderr.strip()}")


def _is_clean(repo: Path) -> bool:
    result = _run_git(repo, "status", "--porcelain", "--untracked-files=no")
    if result.returncode != 0:
        raise PreflightError(f"git status failed: {result.stderr.strip()}")
    return not result.stdout.strip()


def _is_git_repository(repo: Path) -> bool:
    return _run_git(repo, "rev-parse", "--git-dir").returncode == 0


def _canonical(path: Path) -> Path:
    return Path(os.path.normcase(path.expanduser().resolve(strict=False)))


def _paths_overlap(child: Path, parent: Path) -> bool:
    try:
        _canonical(child).relative_to(_canonical(parent))
        return True
    except ValueError:
        return False


def _validate_authority_paths(paths: dict[str, Path]) -> None:
    for a, b in _OVERLAP_PAIRS:
        pa, pb = paths.get(a), paths.get(b)
        if pa is None or pb is None:
            continue
        if _paths_overlap(pa, pb):
            raise PreflightError(f"{a}_overlaps_{b}")
        if _paths_overlap(pb, pa):
            raise PreflightError(f"{b}_overlaps_{a}")


def repository_preflight(
    repository_path: Path,
    *,
    state_path: Path | None = None,
    source_of_truth_path: Path | None = None,
    secret_path: Path | None = None,
    controller_authority_path: Path | None = None,
    base_branch: str = "main",
) -> PreflightResult:
    repo = Path(repository_path).expanduser().resolve()
    if not repo.exists() or not _is_git_repository(repo):
        raise PreflightError("repository_missing")

    paths: dict[str, Path] = {"repository_path": repo}
    if state_path is not None:
        paths["state_path"] = Path(state_path).expanduser().resolve()
    if source_of_truth_path is not None:
        paths["source_of_truth_path"] = Path(source_of_truth_path).expanduser().resolve()
    if secret_path is not None:
        paths["secret_path"] = Path(secret_path).expanduser().resolve()
    if controller_authority_path is not None:
        paths["controller_authority_path"] = Path(controller_authority_path).expanduser().resolve()

    _validate_authority_paths(paths)

    name = _git_config(repo, "user.name")
    email = _git_config(repo, "user.email")
    identity: tuple[str, str] | None = (
        (name, email) if name is not None and email is not None else None
    )
    remote_url = _remote_url(repo)

    return PreflightResult(
        identity=identity,
        base_branch_exists=_local_branch_exists(repo, base_branch),
        remote_configured=remote_url is not None,
        remote_url=remote_url,
        clean=_is_clean(repo),
    )
