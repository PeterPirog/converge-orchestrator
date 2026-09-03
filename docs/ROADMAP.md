# Roadmap

The roadmap follows the architecture contract: immutable intent, deterministic policy, isolated
writers, independent review, durable state, evidence, reusable project configuration and
exception-based HITL.

See [CONVERGENCE_AUDIT.md](CONVERGENCE_AUDIT.md) for the current gap analysis against the reference
autonomous-agent architecture.

## v0.1 — executable local core — complete

- immutable requirement hash guard;
- Markdown contract extraction with source-line traceability;
- LangGraph workflow and SQLite checkpoints;
- isolated Git worktrees;
- OpenCode planner/builder/reviewer roles;
- deterministic quality gates;
- bounded repair/replan loops;
- HITL interrupt;
- commit and push of approved work through deterministic integration code.

## v0.2 — GitHub gate and evidence layer — substantially complete

Implemented:

- structured contract root with source path and SHA-256;
- stable requirement IDs and PASS/PARTIAL/FAIL/UNVERIFIED/BLOCKED vocabulary;
- Task Envelope scope controls (`allowed_paths`, diff limit, risk flags);
- deterministic policy engine separated from LLM output;
- evidence store (`task.json`, diff, quality, review, PR, CI and event log);
- GitHub adapter using authenticated `gh api` transport;
- automatic PR creation, CI observation and optional merge;
- cleanup/abandon path for replanning;
- CI matrix on Python 3.11–3.13.

Still useful hardening:

- GitHub issue/backlog synchronization;
- branch-protection/required-check discovery rather than treating every reported check as relevant;
- explicit remote-origin validation;
- resilient CI polling/checkpoint scheduling for multi-hour workflows;
- automated branch/worktree garbage collection after crash recovery.

## v0.3 — compliance, stack portability and safety — substantially complete

Implemented:

- requirement-specific deterministic verifier interface;
- baseline vs candidate comparison;
- mandatory PASS-to-non-PASS regression blocking;
- target deterministic progress rule;
- compliance snapshot persisted across runs for unchanged Source of Truth;
- stack-aware quality discovery for Python, Node, Go and Rust;
- normalized missing-tool and timeout failures;
- stable OpenCode read-only Scout/Planner/Reviewer and bounded Builder permissions;
- high-precedence runtime role config so target-repo OpenCode settings cannot weaken role boundaries;
- exact target requirement statements/source anchors injected into Builder/repair prompts;
- parallel independent review coordinator with correctness, architecture and security lanes;
- deterministic review aggregation: any lane rejection or lane execution failure blocks integration;
- reviewer findings stamped with the responsible lane for repair evidence.

Remaining:

- deterministic AST/import architecture rules independent from custom project scripts;
- explicit public-API compatibility adapters;
- destructive migration / secret / public-API risk classifier;
- stronger OS/container sandbox profiles;
- fault-recovery tests for killed orchestrator/OpenCode/CI failures.

## v0.4 — reusable configuration and service/control plane — in progress

Implemented:

- FastAPI project registration, bootstrap, run, status, pause/resume and decision endpoints;
- optional Bearer authentication for all control API routes except liveness `/health`;
- stable per-run LangGraph `thread_id` and checkpoint-aware resume;
- autonomous post-merge next-task loop;
- single user-maintained `converge.yaml` with documented sections;
- paths resolved relative to the YAML file for IDE-independent behavior;
- backward-compatible legacy flat configuration;
- OpenWebUI/OpenAI-compatible generated stable OpenCode provider;
- reusable model profiles and per-agent runtime properties;
- quality-first default routing: DeepSeek V4 Flash Scout, DeepSeek V4 Pro Planner/architecture review,
  Kimi K2.7 Code Builder, GLM 5.3 Flash correctness review and gpt-oss 120B security review;
- model-family diversity documented as a review-quality invariant;
- fast read-only Repo Scout/Triage before Planner with exact-base-commit capture, bounded durable
  snapshot, requirement-hint validation and non-blocking fallback;
- bounded configurable review parallelism with legacy single-reviewer compatibility;
- OpenWebUI native Workspace Tool operator bridge over FastAPI/LangGraph;
- fail-closed interactive confirmation for register/bootstrap/start/pause/resume/HITL decisions;
- read-only OpenWebUI access to run status, compliance, evidence and pending interrupts;
- `converge models` model-catalog discovery;
- model gateway live validation in `converge doctor`;
- OpenCode MCP configuration embedded in the project YAML;
- generated `opencode.generated.json` outside the target repository;
- detailed PyCharm/OpenWebUI/OpenCode onboarding, configuration, model-routing and operator references.

Next priorities:

- context-budget/session-rotation policy for long-running projects;
- sandboxed execution runner with filesystem/network/process policy;
- deterministic TDD evidence policy for behavior-changing tasks;
- risk classifier and public-API/data-migration compatibility adapters;
- provider/model fallback policy with bounded retries and evidence;
- PostgreSQL checkpointer/control registry for multi-worker deployment;
- structured metrics and OpenTelemetry/LangSmith integration;
- E2E fixture repositories and chaos/restart test suite.

## v0.5 — production autonomous operation

Planned:

- recoverable leases/locks for distributed workers;
- stale worktree/branch garbage collection;
- branch-protection and required-check awareness;
- flake-aware CI retry policy without hiding deterministic failures;
- cost/token/time budgets per project and per run;
- model routing/fallback policy with audit trail and per-role health statistics;
- container images and hardened deployment profile;
- multi-project dashboard and operator audit views;
- bounded parallel builders only for scheduler-proven non-overlapping write sets.
