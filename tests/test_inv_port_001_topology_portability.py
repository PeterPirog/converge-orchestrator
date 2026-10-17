"""Lock INV-PORT-001: the canonical LangGraph topology is target stack/model/runtime agnostic.

INV-PORT-001 (CONVERGE_ORCHESTRATOR_2_0_SOURCE_OF_TRUTH.md:L160 [4. Global invariants]):
target-repository language, framework, package manager, test framework, build system, model
vendor, model gateway, and agent execution runtime SHALL NOT be hard-coded into the LangGraph
workflow topology; those concerns belong to dedicated non-topology modules.

These deterministic offline tests pin that invariant on the canonical durable service graph
(``converge_orchestrator.graph_service.build_graph``): the builder signature is stack/vendor/
gateway/runtime free, the compiled node and edge set is a fixed static topology whose names
encode no concrete technology, the two ``build_graph`` composition bodies hardcode no selection
over any of the eight dimensions, stack detection stays in ``inspector`` and gateway/runtime
selection stays in ``model_gateway`` / ``opencode_config`` / ``opencode``. The graph compiles in
memory only; no live model, gateway, network, provider, or subprocess call is made.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from converge_orchestrator.graph import build_graph as build_legacy_graph
from converge_orchestrator.graph_service import build_graph

# Concrete-technology tokens across all eight INV-PORT-001 dimensions (language, framework,
# package manager, test framework, build system, model vendor, model gateway, agent runtime).
_FORBIDDEN_TECHNOLOGY_TOKENS = frozenset(
    {
        "python", "nodejs", "javascript", "typescript", "golang", "rust", "java", "ruby",
        "php", "kotlin", "swift", "csharp", "scala", "dart", "fastapi", "django", "flask",
        "express", "react", "vue", "svelte", "nextjs", "nuxt", "angular", "spring", "rails",
        "pip", "poetry", "npm", "pnpm", "yarn", "cargo", "bun", "gomod", "hatchling",
        "setuptools", "wheel", "webpack", "vite", "rollup", "esbuild", "cmake", "maven",
        "gradle", "pytest", "unittest", "jest", "vitest", "mocha", "jasmine",
        "openai", "anthropic", "gemini", "claude", "cohere", "mistral", "ollama", "deepseek",
        "grok", "openwebui", "openrouter", "litellm", "vllm", "opencode", "auggie", "cline",
        "aider", "codex", "codewave",
    }
)

# Selection/ownership symbols that must stay OUT of the build_graph composition bodies and IN
# the dedicated non-topology modules (inspector for stack; model_gateway / opencode_config /
# opencode for gateway and agent runtime).
_SELECTION_SYMBOLS = (
    "provider_id", "base_url", "model_gateway", "model_profiles", "provider_models",
    "api_key_env", "resolve_agent_model", "resolve_profile_model", "inspect_repository",
    "package_manager", "stackprofile", "pyproject.toml", "package.json", "go.mod", "cargo.toml",
)

# Pinned canonical static topology of graph_service.build_graph (identical for every target).
_CANONICAL_NODES = frozenset(
    {
        "__start__", "__end__", "bootstrap", "build", "ci_poll", "ci_wait", "guard_plan",
        "guard_quality", "human", "integrate", "merge", "pause_build", "pause_integrate",
        "pause_merge", "pause_plan", "pause_pr", "pause_repair", "pause_reviewer_recovery",
        "pause_reviewer_recovery_wait", "pause_tdd_red_repair", "plan", "planner_human", "pr",
        "prepare_worktree", "quality", "refresh", "repair", "replan", "review",
        "review_execution_failure", "reviewer_recovery", "scout", "snapshot_frozen_red",
        "spec_stop", "tdd_baseline", "tdd_human", "tdd_red_build", "tdd_red_gate",
    }
)

_CANONICAL_EDGES = frozenset(
    {
        ("__start__", "bootstrap"), ("bootstrap", "guard_plan"), ("build", "guard_quality"),
        ("ci_poll", "__end__"), ("ci_poll", "ci_wait"), ("ci_poll", "human"),
        ("ci_poll", "pause_merge"), ("ci_poll", "pause_repair"), ("ci_poll", "replan"),
        ("ci_wait", "ci_poll"), ("guard_plan", "__end__"), ("guard_plan", "pause_plan"),
        ("guard_quality", "__end__"), ("guard_quality", "quality"), ("human", "__end__"),
        ("human", "guard_plan"), ("human", "guard_quality"), ("human", "pause_repair"),
        ("human", "prepare_worktree"), ("integrate", "__end__"), ("integrate", "pause_pr"),
        ("integrate", "replan"), ("merge", "refresh"), ("pause_build", "__end__"),
        ("pause_build", "build"), ("pause_build", "tdd_red_build"), ("pause_integrate", "__end__"),
        ("pause_integrate", "integrate"), ("pause_merge", "__end__"), ("pause_merge", "merge"),
        ("pause_plan", "__end__"), ("pause_plan", "scout"), ("pause_pr", "__end__"),
        ("pause_pr", "pr"), ("pause_repair", "__end__"), ("pause_repair", "repair"),
        ("pause_reviewer_recovery", "__end__"), ("pause_reviewer_recovery", "reviewer_recovery"),
        ("pause_reviewer_recovery_wait", "__end__"),
        ("pause_reviewer_recovery_wait", "reviewer_recovery"),
        ("pause_tdd_red_repair", "__end__"), ("pause_tdd_red_repair", "tdd_red_build"),
        ("plan", "__end__"), ("plan", "plan"), ("plan", "planner_human"),
        ("plan", "prepare_worktree"), ("plan", "replan"), ("planner_human", "__end__"),
        ("planner_human", "plan"), ("pr", "ci_poll"), ("prepare_worktree", "tdd_baseline"),
        ("quality", "review"), ("refresh", "__end__"), ("refresh", "guard_plan"),
        ("refresh", "human"), ("repair", "guard_quality"), ("replan", "guard_plan"),
        ("review", "human"), ("review", "pause_integrate"), ("review", "pause_repair"),
        ("review", "pause_reviewer_recovery"), ("review", "pause_reviewer_recovery_wait"),
        ("review", "replan"), ("review", "review_execution_failure"), ("review", "spec_stop"),
        ("review_execution_failure", "__end__"), ("reviewer_recovery", "human"),
        ("reviewer_recovery", "pause_integrate"), ("reviewer_recovery", "pause_reviewer_recovery"),
        ("reviewer_recovery", "pause_reviewer_recovery_wait"), ("reviewer_recovery", "replan"),
        ("reviewer_recovery", "review_execution_failure"), ("reviewer_recovery", "spec_stop"),
        ("scout", "plan"), ("snapshot_frozen_red", "build"),
        ("snapshot_frozen_red", "pause_tdd_red_repair"), ("snapshot_frozen_red", "replan"),
        ("snapshot_frozen_red", "tdd_human"), ("spec_stop", "__end__"),
        ("tdd_baseline", "pause_build"), ("tdd_baseline", "replan"), ("tdd_baseline", "tdd_human"),
        ("tdd_human", "__end__"), ("tdd_human", "replan"), ("tdd_red_build", "tdd_red_gate"),
        ("tdd_red_gate", "snapshot_frozen_red"),
    }
)


def _module_source(name: str) -> str:
    spec = importlib.util.find_spec(f"converge_orchestrator.{name}")
    assert spec is not None and spec.origin is not None, f"missing module {name!r}"
    return Path(spec.origin).read_text(encoding="utf-8")


def _composition_sources() -> dict[str, str]:
    return {
        "graph_service.build_graph": inspect.getsource(build_graph),
        "graph.build_graph": inspect.getsource(build_legacy_graph),
    }


def _forbidden_tokens_in(name: str) -> list[str]:
    lowered = name.lower()
    return sorted(token for token in _FORBIDDEN_TECHNOLOGY_TOKENS if token in lowered)


def test_canonical_builder_signature_exposes_no_target_stack_parameter() -> None:
    for builder in (build_graph, build_legacy_graph):
        params = set(inspect.signature(builder).parameters)
        assert params == {"checkpointer"}, (
            f"{builder.__module__}.{builder.__name__} exposes non-portable parameters: "
            f"{sorted(params)}"
        )
        for param in params:
            assert not _forbidden_tokens_in(param), (
                f"{builder.__name__} parameter {param!r} encodes a concrete technology: "
                f"{_forbidden_tokens_in(param)}"
            )


def test_canonical_topology_pins_the_exact_node_and_edge_sets() -> None:
    graph = build_graph().get_graph()
    nodes = set(graph.nodes)
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert nodes == set(_CANONICAL_NODES), (
        "canonical node set drifted:\n"
        f"  missing    = {sorted(set(_CANONICAL_NODES) - nodes)}\n"
        f"  unexpected = {sorted(nodes - set(_CANONICAL_NODES))}"
    )
    assert edges == set(_CANONICAL_EDGES), (
        "canonical edge set drifted:\n"
        f"  missing    = {sorted(set(_CANONICAL_EDGES) - edges)}\n"
        f"  unexpected = {sorted(edges - set(_CANONICAL_EDGES))}"
    )


def test_canonical_node_names_encode_no_concrete_technology() -> None:
    graph = build_graph().get_graph()
    names = {name for name in graph.nodes if name not in ("__start__", "__end__")}
    offenders = {name: _forbidden_tokens_in(name) for name in names if _forbidden_tokens_in(name)}
    assert not offenders, (
        "a canonical node name encodes a concrete language/framework/package-manager/"
        f"test-framework/build-system/model-vendor/gateway/execution-runtime: {offenders}"
    )


def test_topology_composition_bodies_hardcode_no_selection() -> None:
    markers = {token.lower() for token in _FORBIDDEN_TECHNOLOGY_TOKENS}
    markers |= {symbol.lower() for symbol in _SELECTION_SYMBOLS}
    for label, source in _composition_sources().items():
        lowered = source.lower()
        hits = sorted(marker for marker in markers if marker in lowered)
        assert not hits, (
            f"topology composition {label} hardcodes a selection over a forbidden dimension "
            f"{hits}; it belongs in inspector / model_gateway / opencode_config / opencode"
        )


def test_stack_detection_stays_in_inspector() -> None:
    source = _module_source("inspector")
    for marker in (
        "def inspect_repository", "StackProfile", "package_manager",
        "pyproject.toml", "package.json", "go.mod", "Cargo.toml",
    ):
        assert marker in source, (
            f"stack detection is no longer owned by inspector.inspect_repository "
            f"(missing {marker!r}); INV-PORT-001 keeps stack detection out of the topology"
        )


def test_gateway_and_runtime_selection_stay_in_dedicated_modules() -> None:
    opencode_config = _module_source("opencode_config")
    for marker in (
        "def resolve_profile_model", "def resolve_agent_model", "model_gateway",
        "provider_id", "base_url",
    ):
        assert marker in opencode_config, (
            f"gateway/runtime model selection is no longer owned by opencode_config "
            f"(missing {marker!r})"
        )
    model_gateway = _module_source("model_gateway")
    for marker in (
        "def gateway_model_ids", "def configured_gateway_model_ids", "provider_id", "base_url",
    ):
        assert marker in model_gateway, (
            f"gateway selection is no longer owned by model_gateway (missing {marker!r})"
        )
    opencode = _module_source("opencode")
    assert "class OpenCodeAdapter" in opencode, (
        "the agent execution runtime is no longer owned by the opencode adapter module"
    )
