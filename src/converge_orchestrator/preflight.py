from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import GitHubRepoIdentity
from .remote import _is_github_remote


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    identity: tuple[str, str] | None
    base_branch_exists: bool
    remote_configured: bool
    remote_url: str | None
    clean: bool
    github_identity: GitHubRepoIdentity | None = None


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


def _fetch_github_repo_identity(repo: Path, remote_url: str) -> GitHubRepoIdentity | None:
    """Fetch provider-native GitHub repository identity from GitHub API.

    Uses the gh CLI to fetch repository metadata including the provider-native ID.
    Fails closed if the repository cannot be resolved or metadata is malformed.
    """
    try:
        # Extract owner/repo from remote URL
        from .remote import repository_slug_from_remote
        repo_slug = repository_slug_from_remote(remote_url)
    except Exception as exc:
        raise PreflightError(f"Failed to extract repository slug from remote: {exc}") from exc

    # Use gh CLI to fetch repository metadata
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}", "--jq", "{id,owner.login,name,full_name}"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise PreflightError(
            f"Failed to fetch GitHub repository metadata for {repo_slug}: {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreflightError(f"GitHub API request timed out for {repo_slug}") from exc
    except FileNotFoundError as exc:
        raise PreflightError("gh CLI not found; required for GitHub identity binding") from exc

    try:
        metadata = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise PreflightError(f"GitHub API returned invalid JSON for {repo_slug}") from exc

    # Validate required fields
    provider_native_id = metadata.get("id")
    owner_login = metadata.get("owner", {}).get("login")
    repo_name = metadata.get("name")
    full_name = metadata.get("full_name")

    if not provider_native_id:
        raise PreflightError(f"GitHub API response missing provider-native ID for {repo_slug}")
    if not owner_login:
        raise PreflightError(f"GitHub API response missing owner for {repo_slug}")
    if not repo_name:
        raise PreflightError(f"GitHub API response missing repository name for {repo_slug}")
    if not full_name:
        raise PreflightError(f"GitHub API response missing full_name for {repo_slug}")

    # Validate that the fetched identity matches the expected owner/repo
    expected_owner, expected_repo = repo_slug.split("/", 1)
    if owner_login.casefold() != expected_owner.casefold():
        raise PreflightError(
            f"GitHub API returned owner {owner_login} but remote indicates {expected_owner}"
        )
    if repo_name.casefold() != expected_repo.casefold():
        raise PreflightError(
            f"GitHub API returned repo name {repo_name} but remote indicates {expected_repo}"
        )

    return GitHubRepoIdentity(
        provider="GitHub",
        host="github.com",
        provider_native_id=str(provider_native_id),
        canonical_owner=owner_login,
        canonical_name=repo_name,
        canonical_owner_name=full_name,
    )


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

    # Fetch and bind provider-native GitHub repository identity (only for GitHub remotes)
    github_identity: GitHubRepoIdentity | None = None
    if remote_url and _is_github_remote(remote_url):
        try:
            github_identity = _fetch_github_repo_identity(repo, remote_url)
        except PreflightError:
            # Re-raise preflight errors (fail closed)
            raise
        except Exception as exc:
            # Any other error is a fail-closed condition
            raise PreflightError(f"Failed to bind GitHub repository identity: {exc}") from exc

    return PreflightResult(
        identity=identity,
        base_branch_exists=_local_branch_exists(repo, base_branch),
        remote_configured=remote_url is not None,
        remote_url=remote_url,
        clean=_is_clean(repo),
        github_identity=github_identity,
    )
