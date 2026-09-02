# Architecture

Converge moves an existing Git repository toward an immutable architecture specification through
bounded, auditable iterations. LangGraph owns control flow and durable checkpoints; OpenCode is the
repository-aware coding runtime; deterministic tools and GitHub CI provide evidence. An LLM never
acts as the policy engine.

## Trust hierarchy

1. **Architecture Markdown** — immutable source of truth, outside the target repository when possible.
2. **Policy and quality gates** — deterministic rules; an LLM cannot waive a required failure.
3. **Current Git state and CI evidence** — re-read from tools instead of long chat history.
4. **Agent output** — useful interpretation, always subject to schema validation and policy.

The SHA-256 of the specification is pinned at bootstrap and checked again before every repository
modification boundary. `contract.json` is an index with source anchors, not a replacement for the
original Markdown.

## Execution topology

```text
architecture.md [READ ONLY]
        |
SpecGuard + Contract Compiler
        |
contract.json + provisional compliance
        |
LangGraph state machine + SQLite checkpoints
  |              |              |
planner        builder        reviewer     <-- OpenCode roles
                  |
             git worktree
                  |
        diff-scope + quality gates
                  |
         independent review
            /            \
      repair/replan    policy PASS
                           |
                       commit/push
                           |
                       GitHub PR
                           |
                       GitHub CI
                      /         \
                 repair       merge/stop
                                 |
                         evidence + compliance
```

## Agent boundaries

- **Planner** is read-only and emits one bounded `TaskEnvelope`.
- **Builder** is the sole writer in its worktree and may not push or merge.
- **Reviewer** starts from a fresh read-only context and reviews requirement IDs, acceptance criteria,
  deterministic gate results and the actual diff.
- **Integrator** is deterministic code. It performs commit/push/PR/merge only after policy permits it.

## Task Envelope and scope gate

A task carries requirement IDs, constraints, allowed path patterns, acceptance criteria, a hard diff
budget and risk flags. The orchestrator computes changed paths and diff size itself. A Builder cannot
self-certify scope compliance.

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

`events.jsonl` is an append-only run event stream. This is the beginning of the required audit trail;
large-scale deployments can move metadata to PostgreSQL/object storage without changing agent I/O.

## Compliance

The current compliance engine is deliberately conservative:

- bootstrap: `UNVERIFIED`;
- local deterministic gates + independent review: target requirements become `PARTIAL`;
- green remote CI and successful merge: target requirements become `PASS`.

This is provisional evidence, not a full architecture verifier. Requirement-specific deterministic
verifiers and mandatory-regression comparison remain a v0.3 milestone.

## GitHub integration

The adapter currently uses `gh api`, keeping credentials in the host/GitHub CLI credential store and
out of prompts. It creates PRs, reads check-runs and commit statuses, waits with a bounded timeout and
optionally merges using the configured method. MCP remains appropriate for OpenCode agents that need
read-only GitHub context, but final integration stays in deterministic orchestrator code.

## HITL

Routine test/review/CI failures remain autonomous until repair/replan budgets are exhausted. Human
interruption is also reserved for explicit high-risk flags such as destructive migration, forbidden
public API change, new secret requirement, contradictory requirements or critical auth redesign.
