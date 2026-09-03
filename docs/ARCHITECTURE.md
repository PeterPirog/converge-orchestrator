# Architecture

Converge moves an existing Git repository toward an immutable architecture specification through
bounded, auditable iterations. LangGraph owns control flow and durable checkpoints; stable OpenCode is
the repository-aware coding runtime; deterministic tools and GitHub CI provide evidence. An LLM never
acts as the policy engine.

## Trust hierarchy

1. **Architecture Markdown** — immutable source of truth, outside the target repository.
2. **Policy, requirement verifiers and quality gates** — deterministic rules; an LLM cannot waive a
   required failure.
3. **Current Git state and CI evidence** — re-read from tools instead of long chat history.
4. **Independent review** — semantic evidence for requirements not fully machine-verifiable.
5. **Builder/Planner output** — useful interpretation, always subject to schema validation and policy.

The SHA-256 of the specification is pinned at bootstrap and checked again at repository modification
boundaries. `contract.json` is a traceable index with source anchors, not a replacement for the
original Markdown.

## Configuration plane

A project is configured through one user-maintained `converge.yaml`. The recommended sections are:

```text
project   -> target repo, immutable requirements, state/worktree paths
github    -> repo, base branch, PR/CI/merge policy
opencode  -> stable CLI/server mode, generated config path, MCP
models    -> gateway plus reusable model profiles
agents    -> role -> OpenCode agent + model profile + bounded runtime limits
quality   -> discovery, deterministic gates, requirement verifiers
workflow  -> repair/replan/iteration/diff budgets
```

The runtime normalizes that document into `ProjectConfig`. Older flat configuration remains accepted
for compatibility, but graph topology does not depend on either layout.

The user-facing YAML is the Source of Configuration. Converge materializes derived runtime state under
`state_dir`, never inside the target repository:

```text
converge.yaml
    |
    +--> ProjectConfig
    |      |
    |      +--> LangGraph/runtime policy
    |      +--> quality/verifier policy
    |      +--> GitHub policy
    |
    +--> opencode.generated.json
           |
           +--> stable provider/model catalog
           +--> complete runtime agent role definitions
           +--> model/request overrides
           +--> MCP
```

`opencode.generated.json` is disposable and reproducible. It is not edited manually.

## Model gateway boundary

Converge supports three model-routing modes without changing graph topology:

- `existing`: use provider/model references already configured in OpenCode;
- `openwebui`: generate an OpenAI-compatible OpenCode provider pointing at OpenWebUI;
- `openai_compatible`: generate a provider for another compatible gateway.

Secrets are referenced by environment-variable name only. Generated OpenCode config never serializes
API-key values.

Model profiles are separate from agent roles. A profile describes model/provider plus optional
provider-specific request overlays. Agent roles select profiles and add bounded execution properties
such as timeout and step budget. This allows the same orchestration graph to use different model
portfolios for different projects.

OpenWebUI is currently a **model gateway** integration. A future OpenWebUI operator dashboard is a
separate control-plane concern and must not become durable workflow state.

## Model diversity invariant

Model selection is part of quality engineering, not policy authority. The reference quality-first
routing intentionally uses different model families for generation and review:

```text
Planner   -> deepseek-v4-pro:cloud
Builder   -> kimi-k2.7-code:cloud
Reviewer  -> glm-5.3-flash:cloud
```

The Builder is optimized for long-horizon coding. Planner is optimized for architecture/reasoning.
Reviewer deliberately comes from another model family so review is less likely to reproduce the
Builder's assumptions.

This diversity is **not** sufficient evidence for merge. Model output remains below deterministic
quality gates, requirement verifiers and CI in the trust hierarchy. If models are changed for another
project, preserve the principle where practical: Builder and semantic Reviewer should not be the same
model/family by default.

Future read-only review fan-out should add a second independent family (for example a security reviewer)
rather than turning a single reviewer into a larger prompt.

## Execution topology

```text
architecture.md [READ ONLY]
        |
SpecGuard + Contract Compiler
        |
contract.json + durable compliance
        |
LangGraph state machine + SQLite checkpoints
  |              |              |
planner        builder        reviewer     <-- stable OpenCode roles
                  |
             git worktree
                  |
       scope + requirement verifiers
                  |
       stack-aware quality adapter
                  |
         independent review
            /            \
      repair/replan    policy PASS
                           |
                 deterministic integrator
                           |
                       commit/push
                           |
                       GitHub PR
                           |
                       GitHub CI
                      /         \
                 repair       merge/stop
                                 |
                    refresh main + compliance
                                 |
                         next task/converged
```

## Agent boundaries

- **Planner** is read-only and emits one bounded `TaskEnvelope`.
- **Builder** is the sole writer in its worktree and may not push or merge.
- **Reviewer** starts from a fresh read-only context and reviews requirement IDs, acceptance criteria,
  deterministic gate results and the actual diff.
- **Integrator** is deterministic code. It performs commit/push/PR/merge only after policy permits it.

The checked-in `.opencode/agents` definitions document reference roles, but actual local execution uses
complete role definitions generated by Converge and supplied with higher precedence than target-repo
OpenCode configuration. This prevents a repository-local `opencode.json`/`.opencode` from weakening
Builder/Reviewer safety boundaries.

Project YAML may select models and enable custom/MCP tools, but it cannot override protected role
permissions such as edit, shell integration authority, external directories or task delegation.

`opencode.auto_approve` can auto-approve operations OpenCode would normally classify as `ask`. Explicit
`deny` rules remain denied. This is autonomy convenience, not an OS/container sandbox.

## Requirement-context boundary

The architecture file is outside the writable worktree. The orchestrator reads and hashes Source of
Truth itself. Builder does not receive a mutable copy and is not asked to access the external file.
Instead each build/repair prompt contains the exact requirement statements and source anchors selected
by the validated Task Envelope.

This avoids two failure modes:

- opening filesystem access outside the worktree just so the Builder can read requirements;
- repeated paraphrasing of requirements through model memory/chat history.

The original Markdown remains authoritative; injected excerpts are traceable to its source anchors.

## Task Envelope and scope gate

A task carries requirement IDs, constraints, allowed path patterns, acceptance criteria, a hard diff
budget and risk flags. The orchestrator computes changed paths and diff size itself. A Builder cannot
self-certify scope compliance.

One writer is allowed per worktree. Parallelism is reserved for read-only analysis/review until a
scheduler can prove non-overlapping write sets.

## Quality adapter

Graph topology is stack-independent. `QualityAdapter` selects real process commands from project policy
and conservative repository discovery.

Current discovery recognizes:

- Python metadata and declared pytest/Ruff/mypy tooling;
- Node package scripts and npm/pnpm/yarn lockfiles;
- Go modules;
- Rust Cargo manifests.

Explicit project gates are authoritative. Exit code is the source of truth. Missing tools and timeouts
become deterministic failures rather than model judgments.

## Requirement verification and monotonic convergence

Requirements can optionally be bound to deterministic verifier commands. For a candidate task Converge
compares verifier state against the canonical base checkout.

Integration rules include:

- an existing mandatory verifier `PASS` may not become non-PASS;
- pre-existing baseline failures do not have to be fixed by unrelated tasks;
- if the Task Envelope targets a requirement with configured deterministic evidence, at least one
  configured target must improve from non-PASS to `PASS`;
- requirements without machine-verifiable evidence remain subject to independent semantic review.

This turns monotonic convergence into executable policy instead of a prompt instruction.

## Compliance

Compliance is persisted across runs when the Source of Truth hash is unchanged. Entries use:

```text
PASS
PARTIAL
FAIL
UNVERIFIED
BLOCKED
```

Deterministic requirement verifiers can establish PASS/FAIL evidence. Local general gates and semantic
review can provide supporting evidence but do not invent deterministic proof that does not exist.

After a successful merge the graph refreshes canonical `main`, recomputes compliance and either plans
another bounded task or terminates as converged/budget-exhausted.

## Evidence

Every run owns `state_dir/evidence/<run-id>/`. Task artifacts include:

```text
<task-id>/task.json
<task-id>/diff.patch
<task-id>/quality.json
<task-id>/review.json
<task-id>/pr.json
<task-id>/ci.json
```

`events.jsonl` is an append-only run event stream. Requirement baseline/candidate verifier state is
embedded in quality evidence so the integration decision is auditable.

Large-scale deployments can move metadata to PostgreSQL/object storage without changing the agent I/O
contract.

## Durable control plane

The FastAPI service owns operator-facing project/run control while LangGraph checkpoints remain the
workflow execution source of truth.

The control registry tracks projects and runs independently from OpenWebUI/chat history. A run keeps a
stable LangGraph `thread_id`. Pause is cooperative and happens at explicit safe boundaries. Resume
continues from the same durable checkpoint.

Human approval is not a generic override. A risk-policy interrupt can be approved, edited or rejected;
failed deterministic tests, review or CI cannot be approved away.

## GitHub integration

The deterministic adapter uses `gh api`, keeping credentials in the host/GitHub CLI credential store
and out of prompts. It creates PRs, reads checks/statuses, waits with a bounded timeout and optionally
merges using the configured method.

MCP remains appropriate for agent read-only context, but final integration authority stays in
orchestrator code.

## HITL

Routine test/review/CI failures remain autonomous until repair/replan budgets are exhausted. Human
interruption is reserved for explicit high-risk conditions such as destructive migration, forbidden
public API change, new secret requirement, contradictory requirements, critical auth redesign or
repeated inability to make deterministic progress.

## Remaining hardening boundary

OpenCode permission rules are not a kernel security boundary. Strong autonomous operation still needs a
sandbox profile with explicit filesystem/network/process limits, especially for untrusted repositories.
That remains a dedicated roadmap item rather than being hidden behind model permissions.
