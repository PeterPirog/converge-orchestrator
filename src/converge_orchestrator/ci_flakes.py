from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .github import GitHubAdapter, GitHubError
from .models import CIResult, ProjectConfig

_PASSING_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}
_ACTIONS_JOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/actions/runs/\d+/job/(\d+)(?:/)?$")


class FlakyCIPolicy(BaseModel):
    """Exact opt-in policy for bounded GitHub Actions job reruns."""

    checks: list[str] = Field(default_factory=list)
    max_retries_per_check: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def normalize_checks(self) -> FlakyCIPolicy:
        normalized = [item.strip() for item in self.checks]
        if any(not item for item in normalized):
            raise ValueError("github.flaky_ci.checks must not contain empty names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("github.flaky_ci.checks must not contain duplicates")
        self.checks = normalized
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.checks) and self.max_retries_per_check > 0


def flaky_ci_policy_from_mapping(data: dict[str, Any]) -> FlakyCIPolicy:
    github = data.get("github")
    if not isinstance(github, dict):
        return FlakyCIPolicy()
    raw = github.get("flaky_ci")
    if raw is None:
        return FlakyCIPolicy()
    if not isinstance(raw, dict):
        raise ValueError("github.flaky_ci must be a mapping")
    return FlakyCIPolicy.model_validate(raw)


def load_flaky_ci_policy(path: str | Path) -> FlakyCIPolicy:
    source = Path(path).expanduser().resolve()
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("converge.yaml must contain a YAML mapping at the document root")
    return flaky_ci_policy_from_mapping(data)


def _normalized_failure(item: dict[str, Any]) -> bool:
    kind = item.get("kind")
    if kind == "check_run":
        return (
            item.get("status") == "completed"
            and item.get("conclusion") not in _PASSING_CHECK_CONCLUSIONS
        )
    if kind == "status":
        return item.get("status") not in {"pending", "success"}
    return False


def _authoritative_failures(result: CIResult) -> list[dict[str, Any]]:
    remote = next(
        (item for item in result.checks if item.get("kind") == "remote_policy"),
        None,
    )
    if not isinstance(remote, dict) or not remote.get("authoritative"):
        return []
    has_required_checks = bool(remote.get("required_checks"))
    failures: list[dict[str, Any]] = []
    for item in result.checks:
        if item.get("kind") not in {"check_run", "status"}:
            continue
        if has_required_checks and not item.get("required"):
            continue
        if _normalized_failure(item):
            failures.append(item)
    return failures


def choose_flaky_retry(
    result: CIResult,
    policy: FlakyCIPolicy,
    retry_counts: dict[str, int],
) -> str | None:
    """Return one exact check name to rerun, or None when any failure is non-flaky/ambiguous."""
    if result.status != "fail" or not policy.enabled:
        return None
    failures = _authoritative_failures(result)
    if not failures:
        return None

    names: list[str] = []
    for item in failures:
        if item.get("kind") != "check_run":
            return None
        name = str(item.get("name") or "")
        if name not in policy.checks:
            return None
        if name in names:
            return None
        names.append(name)

    retryable = [
        name
        for name in sorted(names)
        if retry_counts.get(name, 0) < policy.max_retries_per_check
    ]
    if len(retryable) != len(names):
        return None
    return retryable[0] if retryable else None


def _actions_job_id(details_url: str, repo: str) -> int | None:
    from urllib.parse import urlparse

    parsed = urlparse(details_url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    match = _ACTIONS_JOB_PATH.fullmatch(parsed.path)
    if not match:
        return None
    owner, name, raw_job_id = match.groups()
    if f"{owner}/{name}".lower() != repo.lower():
        return None
    return int(raw_job_id)


class GitHubFlakyCIAdapter(GitHubAdapter):
    """Narrow GitHub Actions transport for exact failed check reruns."""

    def rerun_failed_actions_check(self, head_sha: str, check_name: str) -> int:
        payload = self._api_json(f"repos/{self.repo}/commits/{head_sha}/check-runs")
        candidates: list[int] = []
        for check in payload.get("check_runs", []) or []:
            if not isinstance(check, dict) or str(check.get("name") or "") != check_name:
                continue
            if check.get("status") != "completed":
                continue
            if check.get("conclusion") in _PASSING_CHECK_CONCLUSIONS:
                continue
            job_id = _actions_job_id(str(check.get("details_url") or ""), self.repo)
            if job_id is not None:
                candidates.append(job_id)
        unique = sorted(set(candidates))
        if len(unique) != 1:
            raise GitHubError(
                "Flaky CI retry requires exactly one failed GitHub Actions job for "
                f"check {check_name!r}; found {len(unique)}"
            )
        job_id = unique[0]
        self._gh(
            ["api", "--method", "POST", f"repos/{self.repo}/actions/jobs/{job_id}/rerun"],
            timeout=60,
        )
        return job_id


def retry_evidence(
    *,
    check_name: str,
    job_id: int,
    retry_count: int,
    head_sha: str,
) -> dict[str, Any]:
    return {
        "kind": "flaky_ci_retry",
        "check": check_name,
        "actions_job_id": job_id,
        "retry_count": retry_count,
        "head_sha": head_sha,
    }


def retry_error_text(exc: Exception) -> str:
    return json.dumps(
        {"type": type(exc).__name__, "message": str(exc)},
        ensure_ascii=False,
        sort_keys=True,
    )
