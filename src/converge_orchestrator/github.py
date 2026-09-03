from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .models import CIResult, ProjectConfig, PullRequestInfo
from .shell import run


class GitHubError(RuntimeError):
    pass


class GitHubAdapter:
    """GitHub API adapter using authenticated `gh api` transport."""

    def __init__(self, config: ProjectConfig):
        if not config.github_repo:
            raise GitHubError("github_repo is required for GitHub integration")
        self.config = config
        self.repo = config.github_repo

    def _gh(self, args: list[str], cwd: Path | None = None, timeout: int = 300) -> str:
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
        return json.loads(output or "{}")

    @staticmethod
    def _pull_info(response: dict[str, Any]) -> PullRequestInfo:
        return PullRequestInfo(
            number=int(response["number"]),
            url=response["html_url"],
            head_sha=response["head"]["sha"],
            state=response["state"],
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

    def ci_status(self, head_sha: str) -> CIResult:
        checks_payload = self._api_json(f"repos/{self.repo}/commits/{head_sha}/check-runs")
        status_payload = self._api_json(f"repos/{self.repo}/commits/{head_sha}/status")
        checks = list(checks_payload.get("check_runs", []))
        statuses = list(status_payload.get("statuses", []))
        normalized: list[dict[str, Any]] = []
        terminal_failure = False
        pending = False
        success_conclusions = {"success", "neutral", "skipped"}
        for check in checks:
            item = {
                "kind": "check_run",
                "name": check.get("name"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
            }
            normalized.append(item)
            if check.get("status") != "completed":
                pending = True
            elif check.get("conclusion") not in success_conclusions:
                terminal_failure = True
        for status in statuses:
            item = {
                "kind": "status",
                "name": status.get("context"),
                "status": status.get("state"),
            }
            normalized.append(item)
            if status.get("state") == "pending":
                pending = True
            elif status.get("state") != "success":
                terminal_failure = True
        if terminal_failure:
            state = "fail"
        elif pending or not normalized:
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
