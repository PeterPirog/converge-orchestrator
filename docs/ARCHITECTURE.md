# Architecture

Converge moves an existing Git repository toward an immutable architecture specification through bounded, auditable iterations. The LLM is never the policy engine. LangGraph owns state/control flow; OpenCode performs repository-aware planning, implementation and review; Git and deterministic commands establish evidence.

## Trust hierarchy
1. Architecture Markdown — immutable source of truth, preferably outside the target repository.
2. Policy and quality gates — deterministic; LLM output cannot override a failing required gate.
3. Current repository state — re-read from Git, never trusted from long chat history.
4. Agent output — subject to validation.

The SHA-256 of the specification is pinned at initialization and checked before every planning cycle.

## Execution topology
```text
architecture.md (read-only)
        |
SpecGuard + Contract Compiler
        |
LangGraph state machine
  |        |        |
planner  builder  reviewer  <-- OpenCode roles
           |
      git worktree
           |
 deterministic quality gates
           |
 independent review
           |
      repair / replan
           |
       commit + push
           |
      GitHub PR / CI (v0.2)
```

## Agent boundaries
- planner: read-only; chooses one smallest useful task.
- builder: sole writer in one isolated worktree; code + tests only.
- reviewer: fresh, read-only independent session; evaluates actual diff.

One writer per worktree prevents conflicting edits. Parallelism should be introduced first for read-only reviewers.

## State and memory
LangGraph checkpoints live outside the target repository. Long conversational memory is deliberately avoided. Each agent receives a task envelope, relevant requirements and current repository evidence. The repository itself is durable implementation memory.

## HITL
An interrupt is reached only after repair and replan budgets are exhausted. Routine test failures and reviewer rejection remain autonomous. Production policy should also interrupt for destructive migrations, public API breakage, new secrets and contradictory requirements.
