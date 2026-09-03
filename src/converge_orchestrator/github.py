from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .models import CIResult, ProjectConfig, PullRequestInfo
from .remote import RemoteValidationError, validate_origin_repository
from .shell import run


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"context": self.context, "app_id": self.app_id}


@dataclass(frozen=True)
class RemotePolicy:
    base_branch: str
    protected: bool
    authoritative: bool
    source: str
    required_checks: tuple[RequiredCheck, ...] = ()
    strict: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_branch": self.base_branch,
            "protected": self.protected,
            "authoritative": self.authoritative,
            "source": self.source,
            "strict": self.strict,
            "required_checks": [item.as_dict() for item in self.required_checks],
        }


def _required_checks(payload: dict[str, Any]) -> tuple[RequiredCheck, ...]:
    checks: list[RequiredCheck] = []
    covered_contexts: set[str] = set()
    for item in payload.get("checks", []) or []:
        if not isinstance(item, dict) or not item.get("context"):
            continue
        context = str(item["context"])
        raw_app_id = item.get("app_id")
        app_id = int(raw_app_id) if raw_app_id is not None else None
        check = RequiredCheck(context=context, app_id=app_id)
        if check not in checks:
            checks.append(check)
        covered_contexts.add(context)
    for raw_context in payload.get("contexts", []) or []:
        context = str(raw_context)
        if not context or context in covered_contexts:
            continue
        check = RequiredCheck(context=context)
        if check not in checks:
            checks.append(check)
    return tuple(checks)


def _check_run_app_id(check: dict[str, Any]) -> int | None:
    app = check.get("app")
    if not isinstance(app, dict) or app.get("id") is None:
        return None
    return int(app["id"])


def _check_run_state(check: dict[str, Any]) -> str:
    if check.get("status") != "completed":
        return "pending"
    if check.get("conclusion") in {"success", "neutral", "skipped"}:
        return "pass"
    return "fail"


def _commit_status_state(status: dict[str, Any]) -> str:
    state = status.get("state")
    if state == "pending":
        return "pending"
    if state == "success":
        return "pass"
    return "fail"


class GitHubAdapter:
    """GitHub API adapter using authenticated `gh api` transport."""

    def __init__(self, config: ProjectConfig):
        if not config.github_repo:
            raise GitHubError("github_repo is required for GitHub integration")
        self.config = config
        self.repo = config.github_repo
        self._origin_validated = False

    def _validate_origin(self) -> None:
        if self._origin_validated:
            return
        try:
            validate_origin_repository(self.config.repo_path, self.repo)
        except RemoteValidationError as exc:
            raise GitHubError(str(exc)) from exc
        self._origin_validated = True

    def _gh(self, args: list[str], cwd: Path | None = None, timeout: int = 300) -> str:
        self._validate_origin()
        working_dir = cwd or self.config.repo_path
        result = run([self.config.github_binary, *args], cwd=working_dir, timeout=timeout)
        if result.returncode != 0:
            raise GitHubError(result.stdout)
        return result.stdout.strip()

    def _api_json(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> dict[str, Any]:
        args = ["api", endpoint]
        if method != "GET":
            args += ["--method", method]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        output = self._gh(args, timeout=timeout)
        payload = json.loads(output or "{}")
        if not isinstance(payload, dict):
            raise GitHubError(f"GitHub endpoint returned a non-object payload: {endpoint}")
        return payload

    @staticmethod
    def _pull_info(response: dict[str, Any]) -> PullRequestInfo:
        return PullRequestInfo(
            number=int(response["number"]),
            url=response["html_url"],
            head_sha=response["head"]["sha"],
            state=response["state"],
        )

    def remote_policy(self, base_branch: str) -> RemotePolicy:
        encoded_branch = quote(base_branch, safe="")
        branch = self._api_json(f"repos/{self.repo}/branches/{encoded_branch}")
        protected = bool(branch.get("protected"))
        if not protected:
            return RemotePolicy(
                base_branch=base_branch,
                protected=False,
                authoritative=True,
                source="branch_unprotected",
            )

        protection = branch.get("protection")
        if isinstance(protection, dict) and "required_status_checks" in protection:
            summary = protection.get("required_status_checks")
            if summary is None:
                return RemotePolicy(
                    base_branch=base_branch,
                    protected=True,
                    authoritative=True,
                    source="branch_summary",
                )
            if isinstance(summary, dict):
                return RemotePolicy(
                    base_branch=base_branch,
                    protected=True,
                    authoritative=True,
                    source="branch_summary",
                    required_checks=_required_checks(summary),
                    strict=summary.get("strict"),
                )

        try:
            detailed = self._api_json(
                f"repos/{self.repo}/branches/{encoded_branch}/"
                "protection/required_status_checks"
            )
        except GitHubError:
            return RemotePolicy(
                base_branch=base_branch,
                protected=True,
                authoritative=False,
                source="protected_policy_unavailable",
            )
        return RemotePolicy(
            base_branch=base_branch,
            protected=True,
            authoritative=True,
            source="branch_protection",
            required_checks=_required_checks(detailed),
            strict=detailed.get("strict"),
        )

    def find_open_pull_request(self, *, head: str, base: str) -> PullRequestInfo | None:
        """Find the unique open PR for this task branch after a checkpoint race/crash."""
        owner, separator, _ = self.repo.partition("/")
        if not separator:
            raise GitHubError(f"Invalid github_repo: {self.repo}")
        query = urlencode({"state": "open", "head": f"{owner}:{head}", "base": base})
        output = self._gh(["api", f"repos/{self.repo}/pulls?{query}"])
        payload = json.loads(output or "[]")
        if not isinstance(payload, list):
            raise GitHubError("GitHub pull request search returned a non-list payload")
        if len(payload) > 1:
            raise GitHubError(f"Multiple open pull requests found for branch {head}")
        if not payload:
            return None
        if not isinstance(payload[0], dict):
            raise GitHubError("GitHub pull request search returned an invalid item")
        return self._pull_info(payload[0])

    def create_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        response = self._api_json(
            f"repos/{self.repo}/pulls",
            method="POST",
            fields={"title": title, "head": head, "base": base, "body": body},
        )
        return self._pull_info(response)

    def ensure_pull_request(
        self,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        existing = self.find_open_pull_request(head=head, base=base)
        if existing is not None:
            return existing
        return self.create_pull_request(head=head, base=base, title=title, body=body)

    def get_pull_request(self, number: int) -> PullRequestInfo:
        response = self._api_json(f"repos/{self.repo}/pulls/{number}")
        return self._pull_info(response)

    def close_pull_request(self, number: int) -> PullRequestInfo:
        response = self._api_json(
            f"repos/{self.repo}/pulls/{number}",
            method="PATCH",
            fields={"state": "closed"},
        )
        return self._pull_info(response)

    def ci_status(
        self,
        head_sha: str,
        policy: RemotePolicy | None = None,
    ) -> CIResult:
        active_policy = policy or self.remote_policy(self.config.base_branch)
        checks_payload = self._api_json(f"repos/{self.repo}/commits/{head_sha}/check-runs")
        status_payload = self._api_json(f"repos/{self.repo}/commits/{head_sha}/status")
        checks = list(checks_payload.get("check_runs", []))
        statuses = list(status_payload.get("statuses", []))
        normalized: list[dict[str, Any]] = [
            {"kind": "remote_policy", **active_policy.as_dict()}
        ]

        required = active_policy.required_checks
        required_states: dict[RequiredCheck, list[str]] = {item: [] for item in required}
        for check in checks:
            name = str(check.get("name") or "")
            app_id = _check_run_app_id(check)
            matched = [
                item
                for item in required
                if item.context == name
                and (item.app_id is None or item.app_id == app_id)
            ]
            state = _check_run_state(check)
            for item in matched:
                required_states[item].append(state)
            normalized.append(
                {
                    "kind": "check_run",
                    "name": name,
                    "app_id": app_id,
                    "status": check.get("status"),
                    "conclusion": check.get("conclusion"),
                    "required": bool(matched),
                }
            )
        for status in statuses:
            name = str(status.get("context") or "")
            matched = [
                item for item in required if item.context == name and item.app_id is None
            ]
            state = _commit_status_state(status)
            for item in matched:
                required_states[item].append(state)
            normalized.append(
                {
                    "kind": "status",
                    "name": name,
                    "status": status.get("state"),
                    "required": bool(matched),
                }
            )

        if not active_policy.authoritative:
            state = "pending"
        elif required:
            terminal_failure = any(
                "fail" in observations for observations in required_states.values()
            )
            pending = any(
                not observations or "pending" in observations
                for observations in required_states.values()
            )
            if terminal_failure:
                state = "fail"
            elif pending:
                state = "pending"
            else:
                state = "pass"
        else:
            observations = [
                _check_run_state(check) for check in checks
            ] + [_commit_status_state(status) for status in statuses]
            if "fail" in observations:
                state = "fail"
            elif "pending" in observations or not observations:
                state = "pending"
            else:
                state = "pass"
        return CIResult(status=state, head_sha=head_sha, checks=normalized)

    def wait_for_ci(self, head_sha: str) -> CIResult:
        deadline = time.monotonic() + self.config.ci_timeout_seconds
        while time.monotonic() < deadline:
            result = self.ci_status(head_sha)
            if result.status in {"pass", "fail"}:
                return result
            time.sleep(self.config.ci_poll_seconds)
        latest = self.ci_status(head_sha)
        return latest.model_copy(update={"status": "timeout"})

    def merge(self, number: int) -> str:
        current = self._api_json(f"repos/{self.repo}/pulls/{number}")
        if current.get("merged"):
            merged_sha = current.get("merge_commit_sha")
            if not merged_sha:
                raise GitHubError("Merged pull request does not expose merge_commit_sha")
            return str(merged_sha)
        response = self._api_json(
            f"repos/{self.repo}/pulls/{number}/merge",
            method="PUT",
            fields={"merge_method": self.config.merge_method},
        )
        if not response.get("merged"):
            raise GitHubError(response.get("message", "GitHub refused the merge"))
        return str(response["sha"])
