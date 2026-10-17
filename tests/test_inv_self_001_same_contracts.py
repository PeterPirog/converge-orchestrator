"""Lock INV-SELF-001: self-development uses the same run contracts.

INV-SELF-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L159 [4. Global invariants]):
Self-development of Converge SHALL use the same project, candidate, gate, review,
integration, evidence, and recovery contracts as development of any other brownfield target.

These deterministic offline tests pin that the canonical durable service graph
(``converge_orchestrator.graph_service.build_graph``) exposes the identical contract nodes
for project, candidate, deterministic gate, independent review, integration, evidence, and
recovery for every target, and that no run-contract module special-cases the orchestrator's
own repository identity. The only self-identity knowledge in the package is confined to the
two external release-acceptance modules, which agree on a single literal so that self vs.
non-self is single-valued across the acceptance surface.

No network, model, subprocess, or git calls are made: the graph is compiled in memory and
the identity checks read module sources from disk.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

from converge_orchestrator import acceptance, acceptance_supervisor
from converge_orchestrator.graph_service import build_graph

# Run-contract modules that must drive an identical run for every brownfield target.
_RUN_CONTRACT_MODULES = (
    "workflow",
    "graph",
    "graph_service",
    "targeting",
    "quality",
    "verification",
    "compliance",
    "workspace_ownership",
    "runtime_service",
)

# The external release-acceptance surface is the only allowed home of self-identity.
_ACCEPTANCE_SURFACE = ("acceptance", "acceptance_supervisor")

_SELF_IDENTITY = "peterpirog/converge-orchestrator"


def _module_source(name: str) -> str:
    spec = importlib.util.find_spec(f"converge_orchestrator.{name}")
    assert spec is not None and spec.origin is not None, f"missing module {name}"
    return Path(spec.origin).read_text(encoding="utf-8")


def test_canonical_graph_exposes_every_contract_node_for_any_target() -> None:
    graph = build_graph().get_graph()
    nodes = set(graph.nodes)

    contract_nodes = {
        # project contract
        "bootstrap",
        "guard_plan",
        # candidate contract
        "prepare_worktree",
        "build",
        # deterministic gate contract
        "quality",
        # independent review contract
        "review",
        # integration contract
        "integrate",
        "pr",
        # recovery contract: repair, replan, checkpointable pauses, refresh
        "repair",
        "replan",
        "refresh",
        "pause_plan",
        "pause_build",
        "pause_repair",
        "pause_integrate",
        "pause_pr",
        "pause_merge",
    }
    missing = contract_nodes - nodes
    assert not missing, f"canonical service graph is missing contract nodes: {sorted(missing)}"

    # The contracts are wired as one chain: project -> candidate -> gate -> review.
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("bootstrap", "guard_plan") in edges
    assert ("build", "guard_quality") in edges
    assert ("quality", "review") in edges


def test_graph_construction_is_target_agnostic() -> None:
    # The canonical graph builder takes no target identity, so self-development and brownfield
    # development compose the identical contract graph (INV-SELF-001).
    parameters = set(inspect.signature(build_graph).parameters)
    assert not (
        parameters
        & {
            "repo",
            "repo_path",
            "github_repo",
            "target",
            "target_repository",
            "project",
            "project_id",
        }
    )


def test_evidence_contract_requires_the_full_task_bundle_artifacts() -> None:
    artifacts = set(acceptance._REQUIRED_TASK_ARTIFACTS)
    assert artifacts == {
        "task.json",
        "diff.patch",
        "quality.json",
        "review.json",
        "risk.json",
        "pr.json",
        "ci.json",
    }


def test_run_contract_modules_never_reference_the_orchestrator_repository() -> None:
    slug = _SELF_IDENTITY.rsplit("/", 1)[-1].lower()
    for name in _RUN_CONTRACT_MODULES:
        source = _module_source(name).lower()
        assert slug not in source, (
            f"run-contract module {name!r} special-cases the orchestrator's own repository"
        )


def test_self_identity_is_confined_to_the_release_acceptance_surface() -> None:
    package_dir = Path(acceptance.__file__).resolve().parent
    literal = _SELF_IDENTITY.lower()
    carriers = {
        path.name
        for path in sorted(package_dir.glob("*.py"))
        if literal in path.read_text(encoding="utf-8").lower()
    }
    assert carriers == {f"{name}.py" for name in _ACCEPTANCE_SURFACE}


def test_acceptance_surface_agrees_on_one_self_identity_literal() -> None:
    assert acceptance._ORCHESTRATOR_REPOSITORY == _SELF_IDENTITY
    assert acceptance_supervisor._ORCHESTRATOR_REPOSITORY == _SELF_IDENTITY
    literal = acceptance._ORCHESTRATOR_REPOSITORY
    # A single, pre-normalized owner/repo slug: case-folding it changes nothing.
    assert literal == literal.lower().strip()
    assert re.fullmatch(r"[a-z0-9-]+/[a-z0-9.-]+", literal)


def test_self_vs_non_self_is_single_valued_across_the_acceptance_surface() -> None:
    repositories = [
        "peterpirog/converge-orchestrator",
        "PeterPirog/converge-orchestrator",
        "PETTERPIROG/Converge-Orchestrator",
        "example/target",
        "converge-orchestrator/peterpirog",
        "peterpirog/converge-orchestrator-fork",
        "other/converge-orchestrator",
    ]
    for repository in repositories:
        # acceptance.evaluate_external_acceptance accepts the target iff it is not self.
        acceptance_external = repository.lower() != acceptance._ORCHESTRATOR_REPOSITORY
        # acceptance_supervisor._validate_acceptance_preconditions rejects self targets.
        supervisor_is_self = repository.lower() == acceptance_supervisor._ORCHESTRATOR_REPOSITORY
        assert acceptance_external == (not supervisor_is_self)
