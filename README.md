# Converge Orchestrator

**Converge** is a requirements-driven autonomous software engineering orchestrator built around
**LangGraph** and **OpenCode**. It repeatedly performs bounded code changes while treating a separate
architecture Markdown file as an immutable source of truth.

The central idea is controlled convergence: nondeterministic coding agents are surrounded by
schema validation, Git worktrees, deterministic quality gates, an independent review path, a policy
engine, GitHub CI and an auditable evidence store.

## Current status — v0.2 development

The repository now implements the local core plus the first GitHub integration layer:

- read-only SHA-256-pinned architecture specification;
- structured `contract.json` with stable requirement IDs and source anchors;
- LangGraph workflow with SQLite checkpoints and HITL;
- OpenCode Planner / Builder / Reviewer roles;
- one writer per isolated Git worktree;
- Task Envelope with path allowlist, diff budget and risk flags;
- deterministic configured gates plus an orchestrator-owned diff-scope gate;
- bounded repair and replan loops;
- independent structured review;
- evidence artifacts and JSONL event stream;
- deterministic GitHub adapter via authenticated `gh api`;
- automatic PR creation and CI observation;
- optional merge only after local gates, review and remote CI pass.

See [Architecture](docs/ARCHITECTURE.md) and [Roadmap](docs/ROADMAP.md).

## Requirements

- Python 3.11+ (CI covers 3.11, 3.12 and 3.13);
- Git;
- OpenCode available as `opencode` or a reachable OpenCode server;
- GitHub CLI (`gh`) authenticated for GitHub integration;
- an existing local clone with `origin`;
- a separate Markdown architecture/specification file.

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
```

## Configure

Copy `examples/converge.yaml` outside the target repository and set absolute paths. Keep secrets out
of YAML. OpenCode/provider and GitHub credentials should be configured in their native stores.

The requirements file is expected to be OS-level read-only by default:

```bash
chmod 444 /path/to/architecture.md
```

## Validate setup

```bash
converge doctor --config /path/to/converge.yaml
```

## Run a convergence iteration

```bash
converge run --config /path/to/converge.yaml --thread-id payments-main
```

The graph performs:

```text
bootstrap
 -> verify specification hash
 -> refresh base branch
 -> OpenCode Planner emits one Task Envelope
 -> create isolated worktree
 -> OpenCode Builder implements + tests
 -> verify spec hash again
 -> deterministic diff-scope + project quality gates
 -> independent OpenCode Reviewer
    -> failure: bounded repair / fresh replan / HITL
    -> pass: commit + push
 -> GitHub PR
 -> bounded CI observation
    -> failure: repair/replan/HITL
    -> pass: leave ready for merge, or merge when auto_merge=true
```

## Evidence layout

State is outside the target repository by default:

```text
.converge/
├── requirements.sha256
├── contract.json
├── langgraph.sqlite
├── evidence/
│   └── <run-id>/
│       ├── events.jsonl
│       └── <task-id>/
│           ├── task.json
│           ├── diff.patch
│           ├── quality.json
│           ├── review.json
│           ├── pr.json
│           └── ci.json
└── worktrees/
```

## OpenCode roles and Skills

Reference roles live in `.opencode/agents/`. Skills describe **how to work**; project-specific
requirements describe **what the repository must become** and therefore remain in the immutable
Source of Truth rather than global Skills.

Recommended MCP policy is least privilege: read-only GitHub/docs context for Planner/Reviewer and no
blanket GitHub merge permission for Builder. Final integration is intentionally deterministic code.

## Development

```bash
pip install -e '.[dev]'
ruff check .
python -m compileall -q src tests
pytest --cov=converge_orchestrator
```

## Explicit limitations

Converge does not yet claim full architectural compliance. The current compliance state uses
conservative provisional evidence; requirement-specific verifier plugins and mandatory-regression
comparison are the next major safety layer. The API/OpenWebUI control plane, PostgreSQL state and
container sandboxing are also still roadmap items.

The system is intentionally explicit about these boundaries: autonomous development should fail
visibly rather than silently turn model confidence into a merge decision.
