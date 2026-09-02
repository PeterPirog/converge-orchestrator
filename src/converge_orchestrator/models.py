from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class RequirementStatus(StrEnum):
    UNKNOWN = "unknown"
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"


class Requirement(BaseModel):
    id: str
    statement: str
    source: str
    severity: Literal["mandatory", "recommended"] = "mandatory"
    status: RequirementStatus = RequirementStatus.UNKNOWN
    evidence: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model: str | None = None
    agent: str
    timeout_seconds: int = 1800


class QualityGate(BaseModel):
    name: str
    command: str
    required: bool = True
    timeout_seconds: int = 1800


class ProjectConfig(BaseModel):
    repo_path: Path
    requirements_path: Path
    github_repo: str | None = None
    base_branch: str = "main"
    state_dir: Path | None = None
    worktree_dir: Path | None = None
    opencode_binary: str = "opencode"
    opencode_attach_url: str | None = None
    agents: dict[str, AgentConfig]
    quality_gates: list[QualityGate] = Field(default_factory=list)
    max_repair_attempts: int = 3
    max_replans: int = 2
    max_iterations: int = 50
    auto_merge: bool = False

    @model_validator(mode="after")
    def derive_paths(self) -> "ProjectConfig":
        repo = self.repo_path.expanduser().resolve()
        self.repo_path = repo
        self.requirements_path = self.requirements_path.expanduser().resolve()
        self.state_dir = (self.state_dir or repo.parent / ".converge").expanduser().resolve()
        self.worktree_dir = (self.worktree_dir or self.state_dir / "worktrees").expanduser().resolve()
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
    acceptance: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "medium"


class WorkflowState(TypedDict, total=False):
    config_path: str
    requirements_hash: str
    requirements: list[dict[str, Any]]
    iteration: int
    task: dict[str, Any] | None
    worktree: str | None
    branch: str | None
    quality_results: list[dict[str, Any]]
    review_result: dict[str, Any] | None
    repair_attempts: int
    replan_attempts: int
    status: str
    message: str
