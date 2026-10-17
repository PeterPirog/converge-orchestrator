"""Lock INV-REMOTE-001: branch protection is an additional external enforcement layer.

INV-REMOTE-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L161 [4. Global invariants]):
Remote branch protection SHALL be treated as an additional external enforcement layer rather
than the source of Converge's local acceptance semantics.

These deterministic offline tests pin both sides of that boundary:

* confinement — the branch-protection policy vocabulary (``RemotePolicy``, ``RequiredCheck``,
  ``remote_policy``, ``required_checks``, ``branch_protection``, ``branch_unprotected``) is
  carried by exactly the four external-enforcement-surface modules (``github``, ``acceptance``,
  ``acceptance_cli``, ``ci_flakes``) and by no local CI-loop module;
* local acceptance — ``GitHubAdapter.ci_status`` returns ``pass`` for an authoritative
  unprotected policy with a passing check and zero required checks, and the local router
  (``ci.route_after_ci``) accepts that pass without any branch-protection input;
* additional layer — the external release gate (``acceptance._authoritative_ci_policy``) is
  strictly stricter: it demands at least one authoritative required check, so branch
  protection adds enforcement on top of local semantics instead of defining them.

No network, model, subprocess, or git calls: the GitHub transport is stubbed in memory and the
carrier scan reads module sources from disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import converge_orchestrator
from converge_orchestrator.acceptance import _authoritative_ci_policy
from converge_orchestrator.ci import route_after_ci
from converge_orchestrator.github import GitHubAdapter, RemotePolicy, RequiredCheck
from converge_orchestrator.models import ProjectConfig, WorkflowState

# Branch-protection policy vocabulary: the policy/check types, the evidence kind marker, the
# required-check payload key, and the two branch-protection policy source literals.
_BRANCH_PROTECTION_TOKENS = (
    "RemotePolicy",
    "RequiredCheck",
    "branch_protection",
    "branch_unprotected",
    "remote_policy",
    "required_checks",
)

# The only allowed home of branch-protection knowledge: the external enforcement surface.
_EXTERNAL_ENFORCEMENT_SURFACE = {"acceptance.py", "acceptance_cli.py", "ci_flakes.py", "github.py"}

# Local CI acceptance chain that decides run outcomes without branch protection.
_LOCAL_LOOP_MODULES = ("ci.py", "workflow.py", "graph.py", "graph_service.py")


def _package_sources() -> dict[str, str]:
    package_dir = Path(converge_orchestrator.__file__).resolve().parent
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(package_dir.glob("*.py"))
    }


def _config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        repo_path=tmp_path,
        requirements_path=tmp_path / "architecture.md",
        github_repo="owner/repo",
        agents={},
    )


class _StubGitHubAdapter(GitHubAdapter):
    def __init__(self, config: ProjectConfig, responses: dict[str, dict]):
        super().__init__(config)
        self.responses = responses

    def _api_json(self, endpoint: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.responses[endpoint]


def _ci_adapter(
    tmp_path: Path,
    *,
    checks: list[dict],
    statuses: list[dict] | None = None,
) -> _StubGitHubAdapter:
    return _StubGitHubAdapter(
        _config(tmp_path),
        {
            "repos/owner/repo/commits/abc/check-runs": {"check_runs": checks},
            "repos/owner/repo/commits/abc/status": {"statuses": statuses or []},
        },
    )


def _unprotected_policy() -> RemotePolicy:
    return RemotePolicy(
        base_branch="main",
        protected=False,
        authoritative=True,
        source="branch_unprotected",
    )


def test_branch_protection_is_confined_to_the_external_enforcement_surface() -> None:
    sources = _package_sources()
    carriers = {
        name
        for name, source in sources.items()
        if any(token in source for token in _BRANCH_PROTECTION_TOKENS)
    }
    assert carriers == _EXTERNAL_ENFORCEMENT_SURFACE, (
        "branch protection leaked outside the external enforcement surface "
        f"(github/acceptance/acceptance_cli/ci_flakes): {sorted(carriers)}"
    )


def test_github_module_is_the_single_definition_site_of_branch_protection() -> None:
    source = _package_sources()["github.py"]
    missing = [token for token in _BRANCH_PROTECTION_TOKENS if token not in source]
    assert not missing, f"github.py no longer defines the branch-protection policy: {missing}"


def test_local_ci_acceptance_chain_has_no_branch_protection_knowledge() -> None:
    sources = _package_sources()
    offenders = {
        name: [token for token in _BRANCH_PROTECTION_TOKENS if token in sources[name]]
        for name in _LOCAL_LOOP_MODULES
    }
    offenders = {name: hits for name, hits in offenders.items() if hits}
    assert not offenders, (
        "the local CI acceptance chain consults branch protection, which must remain an "
        f"external-only enforcement layer: {offenders}"
    )


def test_ci_status_passes_without_branch_protection(tmp_path: Path) -> None:
    adapter = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "success"}],
    )

    result = adapter.ci_status("abc", _unprotected_policy())

    assert result.status == "pass"
    recorded = next(item for item in result.checks if item.get("kind") == "remote_policy")
    assert recorded["protected"] is False
    assert recorded["authoritative"] is True
    assert recorded["required_checks"] == []


def test_local_router_accepts_passing_ci_without_branch_protection(tmp_path: Path) -> None:
    state: WorkflowState = {"ci": {"status": "pass", "head_sha": "abc", "checks": []}}
    state["config_path"] = "converge.yaml"
    with patch("converge_orchestrator.ci.load_config", return_value=_config(tmp_path)):
        assert route_after_ci(state) == "end"
    with patch(
        "converge_orchestrator.ci.load_config",
        return_value=_config(tmp_path).model_copy(update={"auto_merge": True}),
    ):
        assert route_after_ci(state) == "merge"


def test_external_gate_is_stricter_than_local_acceptance_for_unprotected_branch(
    tmp_path: Path,
) -> None:
    adapter = _ci_adapter(
        tmp_path,
        checks=[{"name": "CI", "status": "completed", "conclusion": "success"}],
    )
    result = adapter.ci_status("abc", _unprotected_policy())

    # Local acceptance semantics: pass, with zero required checks recorded.
    assert result.status == "pass"
    # Additional external enforcement layer: the release gate still demands at least one
    # authoritative required check, so it is strictly stricter than local acceptance.
    ok, count, source = _authoritative_ci_policy(result.model_dump(mode="json"))
    assert (ok, count, source) == (False, 0, "branch_unprotected")


def test_branch_protection_adds_enforcement_that_local_acceptance_lacks(tmp_path: Path) -> None:
    adapter = _ci_adapter(
        tmp_path,
        checks=[
            {"name": "CI", "status": "completed", "conclusion": "success", "app": {"id": 42}},
            {"name": "optional", "status": "completed", "conclusion": "failure", "app": {"id": 42}},
        ],
    )
    policy = RemotePolicy(
        base_branch="main",
        protected=True,
        authoritative=True,
        source="branch_protection",
        required_checks=(RequiredCheck("CI", app_id=42),),
    )

    result = adapter.ci_status("abc", policy)

    # Local acceptance honors the protection policy: the required check passes, the
    # unrequired failure is ignored, and the local gate still opens.
    assert result.status == "pass"
    # The external release gate reads the same authoritative policy as its additional layer.
    ok, count, source = _authoritative_ci_policy(result.model_dump(mode="json"))
    assert (ok, count, source) == (True, 1, "branch_protection")
