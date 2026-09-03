# Converge Orchestrator

**Converge** is a requirements-driven autonomous software engineering orchestrator built around
**LangGraph** and **OpenCode**. It repeatedly performs bounded code changes while treating a separate
architecture Markdown file as an immutable source of truth.

The central idea is controlled convergence: nondeterministic coding agents are surrounded by schema
validation, Git worktrees, deterministic quality gates, requirement-specific verification,
independent review, policy, GitHub CI and an auditable evidence store.

## Start here

For a fresh clone, PyCharm, OpenCode and OpenWebUI setup use:

- [Getting Started: PyCharm + OpenCode + OpenWebUI](docs/GETTING_STARTED.md)
- [Complete `converge.yaml` configuration reference](docs/CONFIGURATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)

The recommended user workflow is deliberately simple: **one project = one `converge.yaml`**.
Repository path, immutable architecture path, GitHub target, model gateway, agent/model properties,
MCP, quality policy and workflow budgets all live in that file. Secrets stay in environment variables.

## Current capabilities

- read-only SHA-256-pinned architecture specification;
- structured `contract.json` with stable requirement IDs and source anchors;
- LangGraph workflow with SQLite checkpoints, cooperative pause points and HITL interrupts;
- autonomous multi-task loop after merge: refresh main, evaluate convergence, plan next bounded task;
- OpenCode Planner / Builder / Reviewer roles;
- OpenCode V2 agent permission profiles with Builder integration authority explicitly denied;
- one writer per isolated Git worktree;
- Task Envelope with path allowlist, diff budget and risk flags;
- deterministic configured gates plus stack-aware Python/Node/Go/Rust quality discovery;
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
- FastAPI endpoints for bootstrap, run status, pause/resume, HITL decision, compliance and evidence;
- single-file project configuration with legacy flat-config compatibility;
- generated OpenCode V2 config outside the target repository;
- OpenWebUI or generic OpenAI-compatible model gateway support;
- per-role model profile, variant, step, timeout and request-body configuration;
- OpenCode MCP configuration embedded in the same `converge.yaml`;
- `converge doctor` validation of paths, Source of Truth, stacks, gates and live gateway model IDs.

## Requirements

- Python 3.11+; CI covers 3.11, 3.12 and 3.13;
- Git;
- OpenCode V2 available as `opencode` or a reachable OpenCode server;
- GitHub CLI (`gh`) when GitHub PR/CI integration is enabled;
- an existing local clone with `origin`;
- a separate Markdown architecture/specification file;
- optional OpenWebUI/OpenAI-compatible gateway and API key for model routing.

## Install

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e '.[dev]'
```

## Recommended filesystem layout

```text
/workspace/
├── converge-orchestrator/
└── payments-target/
    ├── architecture.md          # READ ONLY, immutable Source of Truth
    ├── repository/              # target Git repository
    ├── converge.yaml            # single user-maintained project config
    └── .converge/               # generated state, evidence and worktrees
```

Keep the architecture Markdown outside the target Git repository.

## Configure

Copy the fully commented template:

```bash
cp examples/converge.yaml /path/to/project/converge.yaml
```

The top-level structure is:

```yaml
version: 1
project: {}
github: {}
opencode: {}
models: {}
agents: {}
quality: {}
workflow: {}
```

Example OpenWebUI model routing:

```yaml
models:
  gateway:
    kind: openwebui
    base_url: http://127.0.0.1:3000/api
    api_key_env: OPENWEBUI_API_KEY
  profiles:
    planner:
      model: YOUR_REASONING_MODEL_ID
    builder:
      model: YOUR_CODING_MODEL_ID
    reviewer:
      model: YOUR_REVIEW_MODEL_ID

agents:
  planner:
    agent: converge-planner
    model_profile: planner
  builder:
    agent: converge-builder
    model_profile: builder
  reviewer:
    agent: converge-reviewer
    model_profile: reviewer
```

Set the key in the environment, never in YAML:

```bash
export OPENWEBUI_API_KEY='sk-...'
```

The requirements file is expected to be OS-level read-only by default:

```bash
chmod 444 /path/to/architecture.md
```

## Generated OpenCode configuration

Converge converts the model/MCP/agent runtime part of `converge.yaml` into:

```text
<state_dir>/opencode.generated.json
```

It contains provider metadata, model catalog, agent model/request overrides and MCP configuration.
Only the **name** of the API-key environment variable is written; secret values are never serialized.

Do not edit this generated JSON. Edit `converge.yaml` and run `doctor` again.

## Validate setup

```bash
converge doctor --config /path/to/converge.yaml
```

`doctor` checks the local paths, read-only Source of Truth, tools in PATH, requirement verifier IDs,
stack-aware quality policy, resolved model for every role and—when a gateway is configured—the live
model catalog.

For an intentional offline validation:

```bash
converge doctor --offline --config /path/to/converge.yaml
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
    -> pass: commit + push by deterministic integration layer
 -> GitHub PR
 -> bounded CI observation
    -> failure: repair/replan/HITL
    -> pass: leave ready for merge, or merge when auto_merge=true
 -> after merge: refresh main -> evaluate mandatory compliance -> next task or converged
```

## Requirement-specific deterministic evidence

Projects can bind deterministic commands to requirement IDs without modifying immutable Markdown:

```yaml
quality:
  requirement_verifiers:
    ARCH-017:
      - name: architecture-boundary
        command: [pytest, -q, tests/architecture/test_payment_boundary.py]
        required: true
```

During the task gate Converge runs configured verifiers against the clean canonical base repository and
against the candidate worktree. Existing baseline failures are not treated as newly introduced
regressions, which allows incremental convergence. A mandatory requirement that was `PASS` in the
baseline may not become non-PASS.

Requirements without deterministic evidence remain eligible for independent semantic review; the
orchestrator does not invent a test command merely because a requirement exists.

## Stack-aware quality adapter

When `quality.auto_discover: true`, Converge conservatively discovers commands supported by project
metadata for Python, Node, Go and Rust. Explicit `quality.gates` remain authoritative. Missing tools
and timeouts are normalized into deterministic gate failures rather than converted into model opinion.

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
├── opencode.generated.json
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
required scope/convergence gate stored in `quality.json`, so an integration decision remains auditable.

## OpenCode roles and Skills

Reference roles live in `.opencode/agents/`. Skills describe **how to work**; project-specific
requirements describe **what the repository must become** and remain in the immutable Source of Truth
rather than global Skills.

The user-facing YAML controls model selection and runtime properties. The checked-in role definitions
retain safety invariants such as read-only Planner/Reviewer and no Builder `git push`/`gh` authority.

## Development

```bash
pip install -e '.[dev]'
ruff check .
python -m compileall -q src tests
pytest --cov=converge_orchestrator
pip check
```

## Explicit limitations

Converge does not claim universal architectural compliance. Machine-verifiable requirement evidence is
supported, but semantic requirements still depend on independent review when no deterministic verifier
exists. Strong container/namespace sandboxing, parallel independent correctness/security reviewers,
E2E chaos-recovery fixtures, production PostgreSQL and a dedicated OpenWebUI control-plane UI bridge
remain roadmap work.

OpenWebUI is already supported as the **model gateway** through OpenCode's OpenAI-compatible provider;
that is separate from the future OpenWebUI operator/control dashboard integration.

The system intentionally fails visibly rather than silently turning model confidence into a merge
decision.
