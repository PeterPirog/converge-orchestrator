"""Lock ARCH-CTRL-001: LangGraph owns the durable state machine; Converge owns determinism.

ARCH-CTRL-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L239
[7. Deterministic architecture and agent roles]): LangGraph SHALL implement the durable workflow
state machine while deterministic Converge code SHALL implement target selection, schema
validation, budgets, repository isolation, scope calculation, gate aggregation, compliance
updates, risk transitions, Git integration, cleanup, and recovery.

These deterministic offline tests lock that split: both canonical graph builders compile a
checkpointable StateGraph(WorkflowState) in memory, and every deterministic concern is owned by
a dedicated non-topology Converge module that is wired into the canonical service graph rather
than entangled with model adapters. No network, model, provider, git command, or subprocess call
is made.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from converge_orchestrator.graph import build_graph as build_legacy_graph
from converge_orchestrator.graph_service import build_graph

# Responsibility -> deterministic owner module and authoritative symbol.
# ARCH-CTRL-001 enumerates eleven deterministic concerns; Git integration has two authoritative
# symbols (commit and push) because both are required to land change in the canonical repository.
_RESPONSIBILITY_OWNERS = (
    ("target selection", "targeting", "choose_target_requirement"),
    ("schema validation", "workflow", "_task_envelope_output"),
    ("budgets", "budget", "assert_run_budget"),
    ("repository isolation", "git", "create_worktree"),
    ("scope calculation", "quality", "run_scope_gate"),
    ("gate aggregation", "quality", "required_gates_pass"),
    ("compliance updates", "compliance", "ComplianceEngine"),
    ("risk transitions", "risk", "classify_repository_risk"),
    ("Git integration", "git", "commit_all"),
    ("Git integration", "git", "push"),
    ("cleanup", "git", "cleanup_worktree"),
    ("recovery", "workflow", "reviewer_recovery"),
)

# Lifecycle nodes that every durable Converge graph must expose.
_BASE_LIFECYCLE_NODES = frozenset(
    {
        "__start__",
        "__end__",
        "bootstrap",
        "plan",
        "prepare_worktree",
        "build",
        "quality",
        "review",
        "integrate",
        "pr",
        "merge",
        "refresh",
        "human",
        "repair",
        "replan",
    }
)

# Additional lifecycle nodes present only in the canonical durable service graph.
_SERVICE_LIFECYCLE_NODES = frozenset({"ci_poll", "reviewer_recovery"})

# Canonical service-graph node bindings that wire deterministic owners (not model adapters).
_SERVICE_GRAPH_DETERMINISTIC_BINDINGS = (
    '("prepare_worktree", wf.prepare_worktree)',
    '("plan", targeted_plan)',
    '("quality", quality)',
    '("review", wf.review)',
    '("integrate", wf.integrate)',
    '("pr", wf.create_pr)',
    '("merge", wf.merge_pr)',
    '("refresh", wf.refresh_from_main)',
    '("reviewer_recovery", wf.reviewer_recovery)',
    '("ci_poll", ci_poll)',
)


def _module_source(name: str) -> str:
    spec = importlib.util.find_spec(f"converge_orchestrator.{name}")
    assert spec is not None and spec.origin is not None, f"missing module {name!r}"
    return Path(spec.origin).read_text(encoding="utf-8")


def _function_exists(source: str, symbol: str) -> bool:
    return f"def {symbol}" in source


def _symbol_exists(source: str, symbol: str) -> bool:
    if symbol.startswith("class "):
        return symbol in source
    return _function_exists(source, symbol) or f"class {symbol}" in source


def test_both_graphs_are_checkpointed_stategraphs_over_workflow_state() -> None:
    for builder in (build_legacy_graph, build_graph):
        params = set(inspect.signature(builder).parameters)
        assert "checkpointer" in params, (
            f"{builder.__module__}.{builder.__name__} missing checkpointer parameter"
        )
        source = inspect.getsource(builder)
        assert "StateGraph(WorkflowState)" in source, (
            f"{builder.__module__}.{builder.__name__} does not construct StateGraph(WorkflowState)"
        )
        assert "return graph.compile(checkpointer=checkpointer)" in source, (
            f"{builder.__module__}.{builder.__name__} does not compile with the checkpointer"
        )
        compiled = builder()
        graph = compiled.get_graph()
        nodes = set(graph.nodes)
        missing = _BASE_LIFECYCLE_NODES - nodes
        assert not missing, (
            f"{builder.__module__}.{builder.__name__} missing core lifecycle nodes: "
            f"{sorted(missing)}"
        )

    # The canonical service graph additionally carries durable CI polling and reviewer recovery.
    service_nodes = set(build_graph().get_graph().nodes)
    missing_service = _SERVICE_LIFECYCLE_NODES - service_nodes
    assert not missing_service, (
        f"canonical service graph missing durable lifecycle nodes: {sorted(missing_service)}"
    )


def test_deterministic_concerns_are_owned_by_dedicated_modules() -> None:
    for concern, module, symbol in _RESPONSIBILITY_OWNERS:
        source = _module_source(module)
        assert _symbol_exists(source, symbol), (
            f"ARCH-CTRL-001 concern {concern!r} is no longer owned by {module}.{symbol}: "
            f"symbol not found"
        )


def test_canonical_service_graph_wires_deterministic_owners() -> None:
    source = inspect.getsource(build_graph)
    for binding in _SERVICE_GRAPH_DETERMINISTIC_BINDINGS:
        assert binding in source, (
            f"canonical service graph does not wire deterministic owner {binding}; "
            "ARCH-CTRL-001 requires deterministic modules, not model adapters, to own these nodes"
        )
    assert "OpenCodeAdapter" not in source, (
        "canonical service graph composition directly references a model adapter; "
        "ARCH-CTRL-001 keeps model invocation outside the durable topology"
    )
