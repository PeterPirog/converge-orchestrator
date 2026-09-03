from __future__ import annotations

import enum
import re
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

_REVIEW_AGENT_ROLES = {
    "reviewer",
    "correctness_reviewer",
    "architecture_reviewer",
    "security_reviewer",
}


class RequirementStatus(enum.StrEnum):
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
    context_tokens: int | None = Field(default=None, ge=1)
    output_tokens: int | None = Field(default=None, ge=1)
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


class SandboxConfig(BaseModel):
    """OS/container boundary around model-controlled and repository-controlled execution."""

    mode: Literal["host", "container"] = "host"
    engine: str = "docker"
    image: str | None = None
    agent_network: str = "none"
    quality_network: str = "none"
    agent_gateway_base_url: str | None = None
    require_internal_agent_network: bool = True
    read_only_root: bool = True
    pids_limit: int = Field(default=512, ge=32)
    memory: str | None = "8g"
    cpus: float | None = Field(default=4.0, gt=0)
    tmpfs_size: str = "2g"
    pass_env: list[str] = Field(default_factory=list)
    user: Literal["host", "image"] = "host"

    @model_validator(mode="after")
    def validate_container_policy(self) -> SandboxConfig:
        if self.mode == "container" and not self.image:
            raise ValueError("sandbox.image is required when sandbox.mode=container")
        if (
            self.mode == "container"
            and self.require_internal_agent_network
            and self.agent_network in {"none", "host"}
        ):
            raise ValueError(
                "sandbox requires a named agent_network when "
                "require_internal_agent_network=true"
            )
        if len(self.pass_env) != len(set(self.pass_env)):
            raise ValueError("sandbox.pass_env must not contain duplicates")
        return self


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
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    agents: dict[str, AgentConfig]
    quality_gates: list[QualityGate] = Field(default_factory=list)
    requirement_verifiers: dict[str, list[QualityGate]] = Field(default_factory=dict)
    auto_discover_quality: bool = True
    max_repair_attempts: int = 3
    max_replans: int = 2
    max_iterations: int = 50
    max_diff_lines_hard: int = 1000
    review_roles: list[str] = Field(default_factory=list)
    max_parallel_reviews: int = Field(default=3, ge=1, le=16)
    context_input_fraction: float = Field(default=0.70, gt=0.10, le=0.95)
    context_output_reserve_tokens: int = Field(default=4096, ge=256)
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
            "review_roles",
            "max_parallel_reviews",
            "context_input_fraction",
            "context_output_reserve_tokens",
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

        agent_ids = [agent.agent for agent in self.agents.values()]
        duplicate_agent_ids = sorted(
            {agent_id for agent_id in agent_ids if agent_ids.count(agent_id) > 1}
        )
        if duplicate_agent_ids:
            raise ValueError(
                f"OpenCode agent IDs must be unique across roles: {duplicate_agent_ids}"
            )

        if len(self.review_roles) != len(set(self.review_roles)):
            raise ValueError("workflow.review_roles must not contain duplicates")
        invalid_review_roles = sorted(set(self.review_roles) - _REVIEW_AGENT_ROLES)
        if invalid_review_roles:
            raise ValueError(
                f"unsupported review roles: {invalid_review_roles}; allowed: "
                f"{sorted(_REVIEW_AGENT_ROLES)}"
            )
        missing_review_roles = sorted(set(self.review_roles) - set(self.agents))
        if missing_review_roles:
            raise ValueError(
                f"workflow.review_roles reference unconfigured agents: {missing_review_roles}"
            )
        return self


class AgentResult(BaseModel):
    role: str
    ok: bool
    output: str
    returncode: int = 0
    context: dict[str, Any] | None = None


class GateResult(BaseModel):
    name: str
    ok: bool
    required: bool
    returncode: int
    output: str
    execution: dict[str, Any] | None = None


class RequirementVerification(BaseModel):
    requirement_id: str
    status: RequirementStatus
    evidence: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)


class TDDPlan(BaseModel):
    """Pre-implementation evidence contract for tasks that change observable behavior."""

    mode: Literal["required", "not_applicable"] = "not_applicable"
    test_paths: list[str] = Field(default_factory=list)
    test_gate: str | None = None
    expected_failure_pattern: str | None = None
    rationale: str = ""

    @model_validator(mode="after")
    def validate_required_red_evidence(self) -> TDDPlan:
        if self.mode != "required":
            return self
        if not self.test_paths:
            raise ValueError("tdd.test_paths are required when tdd.mode=required")
        if not self.expected_failure_pattern:
            raise ValueError(
                "tdd.expected_failure_pattern is required when tdd.mode=required"
            )
        if len(self.expected_failure_pattern.strip()) < 4:
            raise ValueError("tdd.expected_failure_pattern is too broad")
        try:
            re.compile(self.expected_failure_pattern)
        except re.error as exc:
            raise ValueError(f"invalid tdd.expected_failure_pattern regex: {exc}") from exc
        return self


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
    change_kind: Literal[
        "behavior",
        "refactor",
        "docs",
        "config",
        "test_only",
        "other",
    ] = "other"
    tdd: TDDPlan = Field(default_factory=TDDPlan)

    @model_validator(mode="after")
    def behavior_changes_require_tdd_contract(self) -> TaskEnvelope:
        if self.change_kind == "behavior" and self.tdd.mode != "required":
            raise ValueError(
                "behavior-changing tasks require tdd.mode=required; plan test infrastructure first "
                "when no deterministic test gate exists"
            )
        return self


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "major", "minor", "note"]
    reason: str
    required_fix: str | None = None
    requirement_id: str | None = None
    file: str | None = None
    line: int | None = None
    reviewer: str | None = None


class ReviewResult(BaseModel):
    verdict: Literal["pass", "reject"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    reviewers: dict[str, Literal["pass", "reject"]] = Field(default_factory=dict)


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
    tdd_baseline_result: dict[str, Any] | None
    tdd_red_result: dict[str, Any] | None
    tdd_red_attempts: int
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
