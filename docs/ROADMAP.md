# Roadmap

The roadmap follows the architecture contract: immutable intent, deterministic policy, isolated
writers, independent review, durable state, evidence, and exception-based HITL.

## v0.1 — executable local core

- immutable requirement hash guard;
- Markdown contract extraction with source-line traceability;
- LangGraph workflow and SQLite checkpoints;
- isolated Git worktrees;
- OpenCode planner/builder/reviewer roles;
- deterministic quality gates;
- bounded repair/replan loops;
- HITL interrupt;
- commit and push of approved work.

## v0.2 — GitHub gate and evidence layer — in progress

Implemented:

- structured contract root with source path and SHA-256;
- stable requirement IDs and PASS/PARTIAL/FAIL/UNVERIFIED/BLOCKED status vocabulary;
- Task Envelope scope controls (`allowed_paths`, diff limit, risk flags);
- deterministic policy engine separated from LLM output;
- evidence store (`task.json`, diff, quality, review, PR, CI and event log);
- GitHub adapter using authenticated `gh api` transport;
- automatic PR creation, CI observation and optional merge;
- cleanup/abandon path for replanning;
- CI matrix on Python 3.11–3.13.

Remaining for v0.2:

- GitHub issue/backlog synchronization;
- branch-protection/required-check discovery rather than treating every reported check as relevant;
- explicit remote-origin validation;
- resilient CI polling/checkpoint scheduling for multi-hour workflows;
- automated branch/worktree garbage collection after crash recovery.

## v0.3 — compliance and safety

- requirement-specific verifier plugin interface;
- deterministic architecture dependency rules;
- baseline/regression comparison and mandatory-regression detection;
- full compliance matrix persisted across runs;
- command allow/deny policy and sandbox profiles;
- destructive migration / secret / public-API risk classifier;
- security reviewer and parallel read-only review fan-out;
- fault-recovery tests for killed orchestrator/OpenCode/CI failures.

## v0.4 — service/control plane

- FastAPI orchestration API: project registration, bootstrap, run, status, pause/resume, decision;
- OpenWebUI bridge/control plane;
- PostgreSQL checkpointer for multi-worker deployment;
- structured metrics and OpenTelemetry/LangSmith integration;
- multi-project stack adapters and project templates;
- bounded parallel builders for non-overlapping modules.
