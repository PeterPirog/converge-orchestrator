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


class ModelGatewayConfig(BaseModel):
    """Model transport used by generated stable OpenCode configuration."""

    kind: Literal["existing", "openwebui", "openai_compatible"] = "existing"
    provider_id: str = "openwebui"
    name: str = "OpenWebUI"
    base_url: str | None = None
    api_key_env: str | None = None
    package: str = "@ai-sdk/openai-compatible"
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def apply_gateway_defaults(self) -> ModelGatewayConfig:
        if self.kind == "openwebui":
            self.base_url = self.base_url or "http://127.0.0.1:3000/api"
            self.api_key_env = self.api_key_env or "OPENWEBUI_API_KEY"
            self.provider_id = self.provider_id or "openwebui"
            self.name = self.name or "OpenWebUI"
        if self.kind == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible gateway requires base_url")
        return self


class ModelProfile(BaseModel):
    """Reusable model selection and provider-specific model options."""

    model: str
    provider: str | None = None
    name: str | None = None
    variant: str | None = None
    request_body: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    agent: str
    model: str | None = None
    model_profile: str | None = None
    variant: str | None = None
    timeout_seconds: int = 1800
    steps: int | None = Field(default=None, ge=1)
    request_body: dict[str, Any] = Field(default_factory=dict)
    tool_permissions: dict[str, Literal["allow", "ask", "deny"]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def model_source_is_unambiguous(self) -> AgentConfig:
        if self.model and self.model_profile:
            raise ValueError("agent may define either model or model_profile, not both")
        return self


class QualityGate(BaseModel):
    name: str
    command: str | list[str]
    required: bool = True
    timeout_seconds: int = 1800
    shell: bool = False


class StackProfile(BaseModel):
    stacks: list[Literal["python", "node", "go", "rust"]] = Field(default_factory=list)
    indicators: dict[str, list[str]] = Field(default_factory=dict)
    quality_gates: list[QualityGate] = Field(default_factory=list)
    package_manager: str | None = None


class ProjectConfig(BaseModel):
    version: Literal[1] = 1
    project_name: str | None = None
    repo_path: Path
    requirements_path: Path
    github_repo: str | None = None
    base_branch: str = "main"
    branch_prefix: str = "converge/"
    state_dir: Path | None = None
    worktree_dir: Path | None = None
    opencode_binary: str = "opencode"
    opencode_attach_url: str | None = None
    opencode_auto_approve: bool = True
    opencode_generated_config_path: Path | None = None
    github_binary: str = "gh"
    model_gateway: ModelGatewayConfig = Field(default_factory=ModelGatewayConfig)
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    mcp: dict[str, Any] = Field(default_factory=dict)
    agents: dict[str, AgentConfig]
    quality_gates: list[QualityGate] = Field(default_factory=list)
    requirement_verifiers: dict[str, list[QualityGate]] = Field(default_factory=dict)
    auto_discover_quality: bool = True
    max_repair_attempts: int = 3
    max_replans: int = 2
    max_iterations: int = 50
    max_diff_lines_hard: int = 1000
    ci_poll_seconds: int = 15
    ci_timeout_seconds: int = 1800
    auto_merge: bool = False
    merge_method: Literal["merge", "squash", "rebase"] = "squash"
    require_spec_read_only: bool = True

    @model_validator(mode="before")
    @classmethod
    def flatten_documented_sections(cls, raw: Any) -> Any:
        """Accept the documented single-file layout while preserving legacy flat config."""
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)

        def copy_value(target: str, section: dict[str, Any], source: str) -> None:
            if target not in data and source in section:
                data[target] = section[source]

        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        copy_value("project_name", project, "name")
        for key in (
            "repo_path",
            "requirements_path",
            "state_dir",
            "worktree_dir",
            "require_spec_read_only",
        ):
            copy_value(key, project, key)

        github = data.get("github") if isinstance(data.get("github"), dict) else {}
        copy_value("github_repo", github, "repo")
        copy_value("github_binary", github, "cli")
        for key in (
            "base_branch",
            "branch_prefix",
            "auto_merge",
            "merge_method",
            "ci_poll_seconds",
            "ci_timeout_seconds",
        ):
            copy_value(key, github, key)

        opencode = data.get("opencode") if isinstance(data.get("opencode"), dict) else {}
        copy_value("opencode_binary", opencode, "binary")
        copy_value("opencode_attach_url", opencode, "attach_url")
        copy_value("opencode_auto_approve", opencode, "auto_approve")
        copy_value("opencode_generated_config_path", opencode, "generated_config_path")
        copy_value("mcp", opencode, "mcp")

        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        copy_value("model_gateway", models, "gateway")
        copy_value("model_profiles", models, "profiles")

        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        copy_value("auto_discover_quality", quality, "auto_discover")
        copy_value("quality_gates", quality, "gates")
        copy_value("requirement_verifiers", quality, "requirement_verifiers")

        workflow = data.get("workflow") if isinstance(data.get("workflow"), dict) else {}
        for key in (
            "max_repair_attempts",
            "max_replans",
            "max_iterations",
            "max_diff_lines_hard",
        ):
            copy_value(key, workflow, key)
        return data

    @model_validator(mode="after")
    def derive_paths_and_validate_profiles(self) -> ProjectConfig:
        repo = self.repo_path.expanduser().resolve()
        self.repo_path = repo
        self.requirements_path = self.requirements_path.expanduser().resolve()
        self.state_dir = (self.state_dir or repo.parent / ".converge").expanduser().resolve()
        self.worktree_dir = (
            self.worktree_dir or self.state_dir / "worktrees"
        ).expanduser().resolve()
        self.opencode_generated_config_path = (
            self.opencode_generated_config_path or self.state_dir / "opencode.generated.json"
        ).expanduser().resolve()

        missing_profiles = sorted(
            {
                agent.model_profile
                for agent in self.agents.values()
                if agent.model_profile and agent.model_profile not in self.model_profiles
            }
        )
        if missing_profiles:
            raise ValueError(f"agents reference unknown model profiles: {missing_profiles}")
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


class RequirementVerification(BaseModel):
    requirement_id: str
    status: RequirementStatus
    evidence: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)


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
    project_id: str
    config_path: str
    run_id: str
    thread_id: str
    requirements_hash: str
    requirements: list[dict[str, Any]]
    baseline: dict[str, Any]
    compliance: dict[str, Any]
    requirement_verifications: list[dict[str, Any]]
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
    approved_risk_flags: list[str]
    status: str
    message: str
