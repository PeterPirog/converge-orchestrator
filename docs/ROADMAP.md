# Roadmap

The roadmap follows the architecture contract: immutable intent, deterministic policy, isolated
writers, independent review, durable LangGraph state, evidence, reusable project configuration and
exception-based HITL.

See [CONVERGENCE_AUDIT.md](CONVERGENCE_AUDIT.md) for the current gap analysis against the reference
autonomous-agent architecture.

## v0.1 — executable local core — complete

- immutable requirement hash guard and traceable Markdown contract extraction;
- LangGraph workflow with SQLite checkpoints;
- isolated Git worktrees and one-writer-per-worktree discipline;
- OpenCode Scout/Planner/Builder/Reviewer roles;
- deterministic quality gates, bounded repair/replan and HITL;
- deterministic commit/push integration path.

## v0.2 — GitHub gate and evidence layer — substantially complete

Implemented:

- structured requirement/compliance/evidence model;
- automatic branch push, PR creation and optional merge through deterministic GitHub code;
- retry-safe PR creation and merge for LangGraph checkpoint races;
- checkpointable CI flow `pr -> ci_poll -> ci_wait -> ci_poll` instead of a blocking polling loop;
- machine-managed CI wait interrupts with durable wake time in LangGraph state;
- automatic CI timer restoration after service restart;
- durable run leases preventing concurrent execution of one LangGraph thread;
- GitHub `origin` validation against configured `github.repo` before project registration and before
  real GitHub transport calls;
- classic branch-protection required-check discovery;
- active GitHub Rulesets `required_status_checks` discovery for the concrete base branch, including
  organization-level effective rules returned by GitHub;
- union of classic and Rulesets required checks with fail-closed policy discovery;
- required-check matching by context and GitHub App/integration ID when GitHub binds the requirement
  to a concrete producer;
- unrelated check runs remain evidence but do not satisfy or fail an authoritative required-check
  policy;
- CI matrix on Python 3.11–3.13.

Remaining hardening:

- stale worktree/branch garbage collection with explicit ownership checks;
- explicit flaky-job classification before any selective CI retry;
- optional issue/backlog synchronization.

## v0.3 — compliance, stack portability and safety — substantially complete

Implemented:

- requirement-specific deterministic verifier interface and baseline/candidate comparison;
- mandatory regression blocking and deterministic target-progress rule;
- persisted compliance for an unchanged Source of Truth;
- stack-aware quality discovery for Python, Node, Go and Rust;
- independent correctness/architecture/security review fan-out with deterministic aggregation;
- common OS/container sandbox for OpenCode, quality gates and requirement verifiers;
- read-only Scout/Planner/Reviewers and the active Builder as the only worktree writer;
- read-only Git metadata, read-only container root, dropped capabilities, no-new-privileges, resource
  limits, tmpfs, allowlisted environment forwarding and controlled networks;
- deterministic TDD baseline, test-only RED, novel failure marker, frozen test hashes and GREEN for
  behavior-changing tasks;
- deterministic final-diff risk classification for secrets, destructive migrations, public Python API
  changes and auth/authz weakening;
- hard-block secret policy before semantic review and risk approval bound to the exact candidate diff.

Remaining:

- deterministic AST/import architecture rules independent from custom project scripts;
- broader cross-language compatibility adapters and safe shim/roll-forward strategies;
- broader chaos suite covering killed service/OpenCode/provider processes and stale resources.

## v0.4 — reusable configuration and service/control plane — substantially complete

Implemented:

- FastAPI project registration, bootstrap, run, status, pause/resume and decision endpoints;
- optional Bearer authentication for control-plane requests;
- stable per-run LangGraph `thread_id`, checkpoint-aware resume and autonomous post-merge loop;
- one user-maintained `converge.yaml`, with paths relative to the YAML file;
- reusable model profiles, per-agent properties, OpenWebUI/OpenAI-compatible gateway configuration,
  MCP configuration, quality/sandbox/workflow settings and documented defaults;
- generated OpenCode configuration outside the target repository;
- OpenWebUI native Workspace Tool operator bridge over FastAPI/LangGraph;
- fresh OpenCode sessions with continuity stored only in LangGraph/evidence;
- deterministic context budgets, authoritative-core no-truncation and advisory-only compaction;
- bounded working-memory artifacts and per-invocation context evidence;
- durable run lease, retry-safe side effects and checkpointable long CI waits;
- detailed PyCharm/OpenWebUI/OpenCode onboarding and sandbox/model-routing documentation.

Next priorities, in order:

1. **Ownership-aware crash/chaos completion** — stale worktree/branch GC plus end-to-end kill/restart
   tests of service, OpenCode, integration checkpoint races and CI wait restoration.
2. **Deterministic architecture analyzers** — AST/import/dependency rules that do not rely solely on
   project-provided scripts or LLM review.
3. **Cross-language compatibility adapters** — public API and migration safety beyond Python plus safe
   shims/roll-forward strategies that reduce HITL.
4. **Provider/model resilience** — bounded fallback/retry policy with evidence and per-role health.
5. **Production state/observability** — PostgreSQL checkpointer/control registry, metrics/tracing,
   backup and multi-worker deployment hardening.

## v0.5 — production autonomous operation

Planned:

- stale worktree/branch garbage collection with explicit ownership and lease/checkpoint checks;
- flake-aware CI retry only for explicitly classified flaky jobs;
- cost/token/time budgets per project and run;
- model routing/fallback audit trail and per-role health statistics;
- project-specific pinned sandbox images and deployment profile;
- structured metrics/OpenTelemetry and optional LangSmith tracing without making external tracing the
  source of evidence;
- multi-project dashboard/operator audit views;
- bounded parallel builders only for scheduler-proven non-overlapping write sets.
