# Converge Orchestrator

**Converge** is a requirements-driven autonomous software engineering orchestrator built around **LangGraph** and **OpenCode**. It repeatedly makes small, testable changes to an existing Git repository while treating a separate architecture Markdown file as an immutable source of truth.

The design goal is not "an LLM that keeps coding". It is a controlled convergence loop in which nondeterministic agents operate inside deterministic Git, test, policy and review boundaries.

## Core principles
- architecture requirements are read-only and SHA-256 pinned;
- LangGraph owns workflow state, retries, checkpointing and HITL;
- OpenCode is the repository-aware planner/builder/reviewer runtime;
- one writer works in one isolated `git worktree`;
- tests/lint/build commands are deterministic quality gates;
- a fresh independent reviewer validates the actual diff;
- repair and replan loops run before a human is interrupted;
- agents never push directly to `main`.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/ROADMAP.md](docs/ROADMAP.md).

## Status
`v0.1` is an executable foundation. A successful iteration plans, builds, validates, reviews, commits and pushes a task branch. Automatic PR creation and CI monitoring are the next adapter layer.

## Requirements
- Python 3.11+
- Git
- OpenCode available as `opencode`
- an existing local clone with an `origin` remote
- a separate Markdown architecture/specification file

## Install
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
```

## Configure
Copy `examples/converge.yaml` outside the target repository and edit absolute paths. Replace quality-gate commands with the target repository's native tests/build/lint commands; Converge does not hard-code pytest.

## Validate setup
```bash
converge doctor --config /path/to/converge.yaml
```

## Run one convergence iteration
```bash
converge run --config /path/to/converge.yaml --thread-id payments-main
```

State/checkpoints are stored by default in a sibling `.converge/` directory, not in the target repository.

## Iteration flow
```text
bootstrap
 -> verify specification hash
 -> update local base from origin
 -> OpenCode planner selects one bounded task
 -> create converge/<task> worktree + branch
 -> OpenCode builder implements + tests
 -> deterministic quality gates
 -> OpenCode reviewer inspects diff
    -> reject: repair -> gates -> review
    -> repeated failure: replan
    -> exhausted budgets: LangGraph interrupt/HITL
    -> approve: commit -> push task branch
```

## OpenCode roles and Skills
Reference profiles are included in `.opencode/agents/`: `converge-planner`, `converge-builder`, and `converge-reviewer`. Reusable Skills in `.opencode/skills/` define *how* agents work. Project requirements stay outside Skills because they define *what* the repository must become.

## Development
```bash
pip install -e '.[dev]'
ruff check .
pytest --cov=converge_orchestrator
```

## Current limitations
- Requirement extraction is traceable but semantic PASS/FAIL evidence is planned for v0.2/v0.3.
- PR creation, Actions polling and auto-merge are the next GitHub adapter layer.
- Planner/reviewer JSON is prompt-constrained rather than schema-enforced.
- Worktree cleanup and stale-branch garbage collection are not yet automated.

These boundaries are explicit because autonomous coding systems should fail visibly rather than imply guarantees they do not implement.
