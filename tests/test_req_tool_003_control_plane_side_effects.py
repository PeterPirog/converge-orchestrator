"""Pin REQ-TOOL-003: external side effects only via deterministic control plane.

Offline assertions pin that remote-mutating entry points (git push,
delete_remote_branch, PR create/ensure/close/merge, Actions rerun) are reachable
only from workflow/ci control-plane modules and git/github/ci_flakes adapters,
and that OpenCode permission envelopes deny git push/gh primitives and never
enable mutating MCP connector families. The AST pin catches names/calls; string
or getattr dispatch is out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import converge_orchestrator
from converge_orchestrator.models import AgentConfig, ProjectConfig, SpecialtyAnalyzer
from converge_orchestrator.opencode_config import build_opencode_config

_REMOTE_MUTATION_TARGETS = frozenset((
    "push", "delete_remote_branch", "ensure_pull_request",
    "create_pull_request", "close_pull_request", "merge",
    "rerun_failed_actions_check",
))
_CONTROL_PLANE_MODULES = frozenset({"workflow.py", "ci.py"})
_TRUSTED_ADAPTER_MODULES = frozenset({"git.py", "github.py", "ci_flakes.py"})
_ALLOWED_SIDE_EFFECT_MODULES = _CONTROL_PLANE_MODULES | _TRUSTED_ADAPTER_MODULES

_MUTATING_CONNECTOR_PREFIXES = (
    "api_", "aws_", "db_", "deploy_", "docker_", "github_"
)
_BUILT_IN_ROLES = (
    "scout", "planner", "builder", "reviewer", "correctness_reviewer",
    "architecture_reviewer", "security_reviewer",
)
_PUSH_GH_COMMANDS = ("git push", "git push *", "gh", "gh *")


class _RemoteMutationVisitor(ast.NodeVisitor):
    def __init__(self, module_name: str, refs: list[tuple[str, str, str]]) -> None:
        self.module_name = module_name
        self.refs = refs

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _REMOTE_MUTATION_TARGETS:
                self.refs.append((self.module_name, alias.name, "import"))
        self.generic_visit(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name in _REMOTE_MUTATION_TARGETS:
            self.refs.append((self.module_name, node.name, "def"))
        self.generic_visit(node)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in _REMOTE_MUTATION_TARGETS:
            self.refs.append((self.module_name, node.func.id, "call"))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _REMOTE_MUTATION_TARGETS
        ):
            self.refs.append((self.module_name, node.func.attr, "call"))
        self.generic_visit(node)


def _collect_remote_mutation_references() -> list[tuple[str, str, str]]:
    pkg_dir = Path(converge_orchestrator.__file__).resolve().parent
    refs: list[tuple[str, str, str]] = []
    for path in pkg_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _RemoteMutationVisitor(path.name, refs).visit(tree)
    return refs


def _project_config(
    tmp_path: Path,
    role: str,
    tool_permissions: dict[str, Any] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    specialty: bool = False,
) -> ProjectConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    requirements = tmp_path / "architecture.md"
    requirements.write_text("System must remain testable.\n", encoding="utf-8")
    specialty_analyzers: dict[str, SpecialtyAnalyzer] = {}
    if specialty:
        specialty_analyzers[role] = SpecialtyAnalyzer(
            description=f"{role} read-only specialty analyzer",
            prompt="Inspect only; do not modify.",
        )
    return ProjectConfig(
        repo_path=repo,
        requirements_path=requirements,
        agents={
            role: AgentConfig(
                agent=f"converge-{role}",
                model="openai/test",
                tool_permissions=tool_permissions or {},
            )
        },
        specialty_analyzers=specialty_analyzers,
        mcp={"servers": mcp_servers or {}},
    )


def test_req_tool_003_remote_mutation_entry_points_are_control_plane_only() -> None:
    violations = [
        (module, name, kind)
        for module, name, kind in _collect_remote_mutation_references()
        if module not in _ALLOWED_SIDE_EFFECT_MODULES
    ]
    assert not violations, (
        "REQ-TOOL-003 gap: remote-mutating side effect referenced outside "
        "declared deterministic control-plane/adapter modules: "
        + "; ".join(f"{kind} {name} in {module}" for module, name, kind in violations)
    )


@pytest.mark.parametrize("role", [*_BUILT_IN_ROLES, "specialty_reviewer"])
def test_req_tool_003_role_permission_envelope_denies_push_and_gh(
    role: str, tmp_path: Path
) -> None:
    is_specialty = role == "specialty_reviewer"
    cfg = _project_config(
        tmp_path,
        role,
        tool_permissions={"docs_*": "allow"},
        mcp_servers={
            "docs": {
                "type": "remote",
                "url": "https://mcp.example.invalid/mcp",
                "enabled": True,
                "oauth": False,
            },
            "github": {
                "type": "remote",
                "url": "https://mcp.example.invalid/gh",
                "enabled": True,
                "oauth": False,
            },
        },
        specialty=is_specialty,
    )
    payload = build_opencode_config(cfg, active_role=role)
    agent_id = cfg.agents[role].agent
    bash = payload["agent"][agent_id]["permission"]["bash"]

    for cmd in _PUSH_GH_COMMANDS:
        effective = bash.get(cmd, bash.get("*", "deny"))
        assert effective == "deny", (
            f"REQ-TOOL-003 gap: role {role!r} does not deny bash primitive {cmd!r} "
            f"(effective={effective!r})"
        )

    mcp = payload.get("mcp", {})
    assert mcp.get("docs", {}).get("enabled") is True, (
        "sanity: read-only docs MCP should remain enabled"
    )
    assert mcp.get("github", {}).get("enabled") is False, (
        f"REQ-TOOL-003 gap: role {role!r} enabled mutating MCP connector family github"
    )

    for key, value in payload["agent"][agent_id]["permission"].items():
        if key in {"bash", "skill"} or not isinstance(value, str):
            continue
        if any(key.startswith(prefix) for prefix in _MUTATING_CONNECTOR_PREFIXES):
            assert value == "deny", (
                f"REQ-TOOL-003 gap: role {role!r} has mutating connector grant "
                f"{key!r}={value!r}"
            )


@pytest.mark.parametrize("role", [*_BUILT_IN_ROLES, "specialty_reviewer"])
@pytest.mark.parametrize("family", _MUTATING_CONNECTOR_PREFIXES)
def test_req_tool_003_mutating_connector_family_grant_fail_closed(
    role: str, family: str, tmp_path: Path
) -> None:
    is_specialty = role == "specialty_reviewer"
    cfg = _project_config(
        tmp_path,
        role,
        tool_permissions={f"{family}*": "allow"},
        specialty=is_specialty,
    )
    with pytest.raises(ValueError) as exc_info:
        build_opencode_config(cfg, active_role=role)
    assert "mutating connector" in str(exc_info.value), (
        f"REQ-TOOL-003 gap: role {role!r} accepted mutating connector grant "
        f"{family!r} without fail-closed rejection"
    )
