from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class RequirementStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


class Requirement(BaseModel):
    id: str
    statement: str
    source: str
    severity: Literal["mandatory", "recommended"] = "mandatory"
    status: RequirementStatus = RequirementStatus.UNVERIFIED
    evidence: list[str] = Field(default_factory=list)


class ContractSource(BaseModel):
    path: str
    sha256: str


class Contract(BaseModel):
    source: ContractSource
    requirements: list[Requirement]


class AgentConfig(BaseModel):
    model: str | None = None
    agent: str
    timeout_seconds: int = 1800


class QualityGate(BaseModel):
    name: str
    command: str | list[str]
    required: bool = True
    timeout_seconds: int = 1800
    shell: bool = False


class ProjectConfig(BaseModel):
    repo_path: Path
    requirements_path: Path
    github_repo: str | None = None
    base_branch: str = "main"
    branch_prefix: str = "converge/"
    state_dir: Path | None = None
    worktree_dir: Path | None = None
    opencode_binary: str = "opencode"
    opencode_attach_url: str | None = None
    github_binary: str = "gh"
    agents: dict[str, AgentConfig]
    quality_gates: list[QualityGate] = Field(default_factory=list)
    max_repair_attempts: int = 3
    max_replans: int = 2
    max_iterations: int = 50
    max_diff_lines_hard: int = 1000
    ci_poll_seconds: int = 15
    ci_timeout_seconds: int = 1800
    auto_merge: bool = False
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    require_spec_read_only: bool = True

    @model_validator(mode="after")
    def derive_paths(self) -> ProjectConfig:
        repo = self.repo_path.expanduser().resolve()
        self.repo_path = repo
        self.requirements_path = self.requirements_path.expanduser().resolve()
        self.state_dir = (self.state_dir or repo.parent / ".converge").expanduser().resolve()
        self.worktree_dir = (
            self.worktree_dir or self.state_dir / "worktrees"
        ).expanduser().resolve()
        return self


class AgentResult(BaseModel):
    role: str
    ok: bool
    output: str
    returncode: int = 0


class GateResult(BaseModel):
    name: str
    ok: bool
    required: bool
    returncode: int
    output: str


class TaskEnvelope(BaseModel):
    id: str
    requirement_ids: list[str]
    title: str
    objective: str
    constraints: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    max_diff_lines: int | None = None
    risk: Literal["low", "medium", "high"] = "medium"
    risk_flags: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "major", "minor", "note"]
    reason: str
    required_fix: str | None = None
    requirement_id: str | None = None
    file: str | None = None
    line: int | None = None


class ReviewResult(BaseModel):
    verdict: Literal["pass", "reject"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class PullRequestInfo(BaseModel):
    number: int
    url: str
    head_sha: str
    state: str = "open"


class CIResult(BaseModel):
    status: Literal["pending", "pass", "fail", "timeout"]
    head_sha: str
    checks: list[dict[str, Any]] = Field(default_factory=list)


class ComplianceEntry(BaseModel):
    requirement_id: str
    status: RequirementStatus
    evidence: list[str] = Field(default_factory=list)


class ComplianceSnapshot(BaseModel):
    entries: dict[str, ComplianceEntry] = Field(default_factory=dict)
    mandatory_regressions: int = 0


class WorkflowState(TypedDict, total=False):
    config_path: str
    run_id: str
    requirements_hash: str
    requirements: list[dict[str, Any]]
    compliance: dict[str, Any]
    iteration: int
    task: dict[str, Any] | None
    worktree: str | None
    branch: str | None
    quality_results: list[dict[str, Any]]
    review_result: dict[str, Any] | None
    repair_attempts: int
    replan_attempts: int
    commit_sha: str | None
    pr: dict[str, Any] | None
    ci: dict[str, Any] | None
    risk_flags: list[str]
    status: str
    message: str
