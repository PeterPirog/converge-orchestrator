# Roadmap

The roadmap follows the architecture contract: immutable intent, deterministic policy, isolated
writers, independent review, durable LangGraph state, evidence, reusable project configuration and
exception-based HITL.

See [CONVERGENCE_AUDIT.md](CONVERGENCE_AUDIT.md) for the current gap analysis against the reference
autonomous-agent architecture.

## v0.1 — executable local core — complete

- immutable requirement hash guard and traceable Markdown contract extraction;
- LangGraph workflow with durable SQLite checkpoints by default;
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
- explicit opt-in flaky GitHub Actions classification with exact check names, a durable per-head retry
  ledger and strict per-check retry caps; mixed, ambiguous or unclassified failures are never retried;
- CI matrix on Python 3.11–3.13;
- ownership-aware stale worktree cleanup with durable resource records and protected active,
  recoverable, interrupted and CI-wait runs.

Remaining hardening:

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
- role-scoped MCP credentials/tools and physically separated Converge-managed Skills, preventing
  unrelated agent roles from inheriting another role's tool instructions or MCP secrets;
- deterministic TDD baseline, test-only RED, novel failure marker, frozen test hashes and GREEN for
  behavior-changing tasks;
- deterministic final-diff risk classification for secrets, destructive migrations, public Python
  API changes, Node package entry-point compatibility and auth/authz weakening;
- hard-block secret policy before semantic review and risk approval bound to the exact candidate diff;
- monotonic Python AST/import boundary enforcement independent from project-provided scripts.

Remaining:

- source-level Node plus Go/Rust compatibility adapters and broader safe roll-forward strategies;
- stale-resource chaos extensions only where a newly discovered failure boundary lacks an equivalent
  deterministic recovery proof.

## v0.4 — reusable configuration and service/control plane — substantially complete

Implemented:

- FastAPI project registration, bootstrap, run, status, pause/resume and decision endpoints;
- optional Bearer authentication for control-plane requests;
- stable per-run LangGraph `thread_id`, checkpoint-aware resume and autonomous post-merge loop;
- one user-maintained `converge.yaml`, with paths relative to the YAML file;
- per-run normalized configuration snapshots with SHA-256 stored in the control registry, so active
  and recovered runs cannot silently change model, gate, retry or merge policy when source YAML changes;
- reusable model profiles, per-agent properties, OpenWebUI/OpenAI-compatible gateway configuration,
  MCP configuration, quality/sandbox/workflow settings and documented defaults;
- generated OpenCode configuration outside the target repository;
- OpenWebUI native Workspace Tool operator bridge over FastAPI/LangGraph;
- fresh OpenCode sessions with continuity stored only in LangGraph/evidence;
- deterministic context budgets, authoritative-core no-truncation and advisory-only compaction;
- bounded working-memory artifacts and per-invocation context evidence;
- durable run lease, retry-safe side effects and checkpointable long CI waits;
- bounded per-role provider retries and ordered model-profile fallback with fresh sessions, unchanged
  permissions, profile-specific context budgets and a durable attempt ledger;
- process-level kill/restart proof after worktree creation preserving uncheckpointed candidate data
  with no duplicate branch/worktree;
- process-level commit/push checkpoint-race proof recovering the exact candidate commit and retrying
  remote push idempotently;
- process-level PR checkpoint-race proof reusing the exact external PR through ensure semantics;
- process-level `ci_wait` restart proof restoring the durable wake timer and automatically resuming the
  same LangGraph run/thread without HITL;
- real subprocess proof that abrupt OpenCode/executor death is absorbed by a bounded primary retry
  with identical role/prompt/model and no hidden session continuation or HITL;
- optional shared PostgreSQL production persistence for both LangGraph checkpoints and the control/run
  registry, with cross-worker atomic leases, strict checkpoint deserialization and focused real-DB CI;
- durable low-cardinality `/diagnostics` and Prometheus `/metrics` reconstructed from the shared control
  registry without process-local counters, model calls or LangGraph routing authority;
- authenticated per-project workload-affinity probe that validates active runs from their pinned
  configuration, allowing an external scheduler to route filesystem-bound work without inspecting or
  mutating LangGraph state;
- fail-closed globally quiesced deployment backup creation/verification covering durable database
  state, Git repositories, immutable requirements and filesystem-backed state/evidence;
- operator `converge-backup create|verify` commands with backend selection inherited from the runtime
  environment and offline integrity verification;
- read-only `converge-backup restore-plan` preflight that re-verifies backup semantics, destination
  emptiness, Git bundle HEAD, storage identities and backend compatibility and emits a plan-bound
  confirmation token without writing deployment state;
- crash-resumable SQLite `restore-apply` with exact confirmation-token enforcement, staged filesystem
  publication, database-last visibility, deterministic target validation and a durable completion
  receipt for final-response recovery and later full-loss reuse;
- detailed PyCharm/OpenWebUI/OpenCode onboarding, persistence, sandbox and model-routing documentation.

Next priorities, in order:

1. **PostgreSQL restore apply** — extend the already-proven preflight/apply contract to PostgreSQL. It
   must keep the no-force operator boundary and prove a deterministic crash-recovery boundary around
   database publication before local journal acknowledgement; the implementation must not rely on an
   ambiguous external-write-then-local-receipt sequence.
2. **Broader language adapters** — source-level Node plus Go/Rust API and dependency rules beyond the
   implemented Python AST and Node package-manifest policies.
3. **Cost/time governance** — bounded project/run budgets and provider-reported telemetry after the
   core autonomous path is operationally hardened.

## v0.5 — production autonomous operation

Planned / partially implemented:

- cost/token/time budgets per project and run;
- provider-reported token/cost telemetry and aggregated per-role health statistics;
- project-specific pinned sandbox images and deployment profile;
- durable registry diagnostics and Prometheus metrics are implemented; OpenTelemetry and optional
  LangSmith tracing may be added without making external tracing the source of evidence;
- multi-project dashboard/operator audit views;
- bounded parallel builders only for scheduler-proven non-overlapping write sets.
