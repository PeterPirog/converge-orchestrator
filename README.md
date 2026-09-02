# Converge Orchestrator

**Converge** is a requirements-driven autonomous software engineering orchestrator built around
**LangGraph** and **OpenCode**. It repeatedly performs bounded code changes while treating a separate
architecture Markdown file as an immutable source of truth.

The central idea is controlled convergence: nondeterministic coding agents are surrounded by
schema validation, Git worktrees, deterministic quality gates, an independent review path, a policy
engine, GitHub CI and an auditable evidence store.

## Current status — v0.3 development

The repository implements the local/GitHub convergence core plus the first durable control-plane API:

- read-only SHA-256-pinned architecture specification;
- structured `contract.json` with stable requirement IDs and source anchors;
- LangGraph workflow with SQLite checkpoints, cooperative pause points and HITL interrupts;
- autonomous multi-task loop after merge: refresh main, evaluate convergence, plan next bounded task;
- OpenCode Planner / Builder / Reviewer roles;
- one writer per isolated Git worktree;
- Task Envelope with path allowlist, diff budget and risk flags;
- deterministic configured gates plus an orchestrator-owned diff-scope gate;
- optional deterministic verifiers bound to concrete requirement IDs;
- monotonic-convergence check: a previously passing mandatory verifier may not regress;
- configured target verifier must improve from non-PASS to PASS before integration;
- bounded repair and replan loops;
- risk approval that cannot waive failed deterministic gates, review or CI;
- independent structured review;
- evidence artifacts, durable compliance snapshot and JSONL event stream;
- deterministic GitHub adapter via authenticated `gh api`;
- automatic PR creation and CI observation;
- optional merge only after local gates, review and remote CI pass;
- SQLite project/run registry independent from chat history;
- FastAPI endpoints for bootstrap, run status, pause/resume, HITL decision, compliance and evidence.

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

### Requirement-specific deterministic evidence

Projects can bind deterministic commands to requirement IDs without modifying the immutable Markdown:

```yaml
requirement_verifiers:
  ARCH-017:
    - name: architecture-boundary
      command: [pytest, -q, tests/architecture/test_payment_boundary.py]
      required: true
```

During the task gate Converge runs those verifiers twice: against the clean canonical base repository
and against the candidate worktree. Existing baseline failures are not treated as newly introduced
regressions, which allows incremental convergence. However, a mandatory requirement that was `PASS`
in the baseline may not become non-PASS. When the active Task Envelope targets a requirement with a
configured verifier, at least one targeted verifier must improve from non-PASS to `PASS`.

Requirements without a deterministic verifier remain eligible for the independent semantic-review
path; the orchestrator does not invent a test command merely because a requirement exists.

## Validate setup

```bash
converge doctor --config /path/to/converge.yaml
```

## Run from CLI

```bash
converge run --config /path/to/converge.yaml --thread-id payments-main
```

The graph performs:

```text
bootstrap
 -> verify specification hash
 -> controlled pause boundary
 -> refresh base branch
 -> OpenCode Planner emits one Task Envelope
 -> create isolated worktree
 -> controlled pause boundary
 -> OpenCode Builder implements + tests
 -> verify spec hash again
 -> diff scope + monotonic requirement verification + project quality gates
 -> independent OpenCode Reviewer
    -> failure: bounded repair / fresh replan / HITL
    -> risk interrupt: explicit human decision without waiving deterministic failures
    -> pass: commit + push
 -> GitHub PR
 -> bounded CI observation
    -> failure: repair/replan/HITL
    -> pass: leave ready for merge, or merge when auto_merge=true
 -> after merge: refresh main -> evaluate mandatory compliance -> next task or converged
```

## Control-plane API

Start the service with:

```bash
converge-api
```

The default bind is `127.0.0.1:8088`; override it with `CONVERGE_API_HOST` and
`CONVERGE_API_PORT`. The control registry defaults to `.converge/control.sqlite` and can be moved
with `CONVERGE_CONTROL_DB`.

MVP endpoints:

```text
POST /projects
POST /projects/{id}/bootstrap
POST /projects/{id}/run
GET  /runs/{id}
POST /runs/{id}/pause
POST /runs/{id}/resume
GET  /runs/{id}/interrupt
POST /runs/{id}/decision
GET  /projects/{id}/compliance
GET  /tasks/{id}/evidence
```

`pause` is cooperative: it writes a durable signal and the graph interrupts at the next explicit safe
boundary. `resume` continues the same LangGraph `thread_id`. Human `approve` is accepted only for
risk-policy interrupts; failed tests, review, architecture policy or GitHub CI cannot be approved away.

## Evidence layout

State is outside the target repository by default:

```text
.converge/
├── requirements.sha256
├── contract.json
├── compliance.json
├── langgraph.sqlite
├── control/
│   └── <run-id>.pause
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

Requirement verifier baseline/candidate states and their exit-code evidence are embedded in the
required `diff_scope` gate stored in `quality.json`, so an integration decision remains auditable.

The API-level project/run registry is a separate SQLite database. This keeps operator state independent
from OpenWebUI/chat history while LangGraph checkpoints remain the source of truth for resumable
workflow execution.

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

Converge still does not claim universal architectural compliance. Deterministic requirement verifiers
are now supported and mandatory PASS-to-non-PASS regressions are blocked when such verifiers exist,
but semantic requirements without machine-checkable evidence still depend on independent review.
Stack-aware quality discovery, richer semi-deterministic AST policies, parallel read-only reviewers,
container sandboxing, the OpenWebUI bridge and production PostgreSQL remain roadmap items.

The control-plane API is intentionally backend-first: OpenWebUI will consume it after the API domain
model stabilizes rather than dictating workflow state through chat history.

The system is intentionally explicit about these boundaries: autonomous development should fail
visibly rather than silently turn model confidence into a merge decision.
