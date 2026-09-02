# Roadmap

## v0.1 — executable core
- immutable requirement hash guard
- Markdown contract compiler with line traceability
- LangGraph workflow and SQLite checkpoints
- isolated Git worktrees
- OpenCode planner/builder/reviewer adapter
- deterministic quality gates
- repair/replan budgets
- Human-in-the-Loop interrupt
- commit and push of approved work

## v0.2 — GitHub control plane
- GitHub MCP/API adapter
- automatic issue/backlog synchronization
- pull-request creation
- CI status observation and bounded retry
- auto-merge policy when protected checks pass
- branch cleanup

## v0.3 — richer compliance engine
- requirement-specific verifier plugins
- architecture dependency rules
- baseline/regression comparison
- compliance matrix with PASS/PARTIAL/FAIL/UNVERIFIED
- monotonic convergence score

## v0.4 — production hardening
- container sandbox profiles
- command allow/deny policy engine
- cost/token budgets
- structured event log and OpenTelemetry
- OpenWebUI operator integration
- concurrent read-only review fan-out
- PostgreSQL checkpointer for multi-worker deployments
