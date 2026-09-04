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
- deterministic Node published-target existence checks: deleting an exact local file that a
  pre-existing package entry still publishes is a compatibility break even when `package.json` is
  unchanged, while compatible retargeting remains review evidence rather than forced HITL;
- Tree-sitter-backed Node direct named-export comparison for exact source modules that remain published
  through an existing package contract; definite removals are blocked while unresolved/ambiguous
  source surfaces remain review evidence instead of guessed failures;
- Tree-sitter-backed TypeScript direct-callable call-shape comparison for the same exact published
  target; a proven increase in the minimum accepted positional arguments is blocked while optional,
  defaulted, rest and plain-JavaScript parameter-list changes do not create guessed HITL;
- bounded local Node wildcard re-export resolution for relative package-confined source graphs, with
  explicit depth/module/edge budgets, cycle/ambiguity fail-conservative behavior and propagation of
  proven named-export and TypeScript minimum-call-arity evidence through unchanged public barrels;
- hard-block secret policy before semantic review and risk approval bound to the exact candidate diff;
- monotonic Python AST/import boundary enforcement independent from project-provided scripts.

Remaining:

- additional high-confidence Node source-signature rules and Go/Rust public API/dependency adapters are
  useful portability work, but must not delay the first production-readiness acceptance gate for the
  explicitly supported Python + conservative Node scope;
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
- deterministic per-run resource envelope pinned in the normalized run configuration, with durable
  crash-safe reservations for model attempts and conservative estimated request/output tokens plus a
  wall-time deadline; retries/fallbacks consume the same finite envelope and budget exhaustion is a
  terminal machine decision rather than HITL;
- process-level kill/restart proof after worktree creation preserving uncheckpointed candidate data
  with no duplicate branch/worktree;
- process-level commit/push checkpoint-race proof recovering the exact candidate commit and retrying
  remote push idempotently;
- process-level PR checkpoint-race proof reusing the exact external PR through ensure semantics;
- process-level `ci_wait` restart proof restoring the durable wake timer and automatically resuming the
  same LangGraph run/thread without HITL;
- checkpointed operator-decision history that excludes machine-managed CI wake-ups, plus an external
  acceptance invariant requiring exactly one predeclared `risk_policy -> approve` decision;
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
- crash-safe PostgreSQL `restore-apply` using an atomic server transaction with a plan-bound server-side
  receipt, database-last publication, exact target binding and real process-death recovery after the
  database commit but before local journal acknowledgement;
- isolated libpq CLI connection environments for PostgreSQL backup/restore so inherited `PG*` target
  settings cannot redirect `pg_dump` or `psql` away from the configured deployment target;
- production container sandbox execution now requires an immutable digest/content-addressed image,
  rejects mutable tags before execution and is exercised by a real Docker CI job with an internal
  network and read-only agent workspace;
- detailed PyCharm/OpenWebUI/OpenCode onboarding, persistence, sandbox and model-routing documentation.

Next priorities, in order:

1. **External repository acceptance** — make the release gate machine-verifiable, then run the complete
   document-to-convergence path against a representative repository outside Converge itself through
   multiple autonomous PR/CI cycles. Deliberately include one controller restart and one exceptional
   HITL condition, require no manual code edits, and require final independent requirements,
   architecture, compatibility, security and evidence checks.
2. **Cross-run economics and forecasting** — provider-reported token/cache/reasoning usage and cost are
   now durably bound to each run-budget reservation and aggregated by run/role/model. Add only
   low-cardinality project/fleet trends or price forecasting that proves operational value; measured
   telemetry must remain secondary to the fail-closed conservative resource envelope.
3. **Broader language adapters** — extend only parser-backed, high-confidence compatibility/dependency
   rules for Node/Go/Rust where the claimed support scope requires them.
4. **Deployment portability follow-up** — deliberately shared/external artifact storage only where
   independent multi-node workers require it; PostgreSQL alone must never imply stateless worker safety.

## First real-repository readiness gate

Converge is already suitable for a **controlled Python repository pilot** when the requirements have
first been normalized and frozen as the authoritative Markdown Source of Truth and the target repository
has meaningful deterministic tests/quality gates. This is a pilot classification, not a claim that an
arbitrary repository can be left unattended indefinitely.

The first general-purpose release for document-driven autonomous repository development should not be
declared ready until all of these executable criteria are met:

1. the claimed autonomous language compatibility scope is explicitly documented and its mandatory
   deterministic compatibility gates are complete; unsupported semantics must fail conservative or be
   delegated to independent review rather than silently guessed;
2. run wall-time and model-use budgets are pinned per durable run, reserved before provider execution,
   survive retries/restarts without counter reset and fail closed with no model/HITL override;
3. a production sandbox image/deployment profile is digest-pinned and exercised in CI;
4. at least one representative **external target repository** passes an acceptance run from frozen
   Markdown requirements through multiple autonomous task/PR/CI cycles to convergence, including one
   controller/process restart, with no manual code edits and HITL only for a deliberately injected
   exceptional condition;
5. the resulting repository is independently checked for requirements compliance, architecture drift,
   compatibility and security, and the evidence bundle can reconstruct why every integrated change was
   accepted.

Criteria 1-3 are implemented for the current declared Python + conservative Node scope. Criterion 4 is
now the primary release blocker; criterion 5 must be proven as part of the same external acceptance
run, not asserted from internal Converge tests.

PDF, DOCX and other authoring formats may be used to prepare requirements, but they must not silently
become parallel workflow authorities. They should be normalized into the reviewed immutable Markdown
requirements artifact before orchestration starts, preserving the architecture's single Source of Truth.

## v0.5 — production autonomous operation

Planned / partially implemented:

- fail-closed per-run wall-time/model-attempt/conservative-token budgets are implemented;
  provider-reported OpenCode usage/cost is persisted per reservation and aggregated by durable
  run/role/model; project-level aggregate spending policy remains future cost-governance work;
- cross-run low-cardinality economics, price forecasting and aggregate per-role health statistics;
- digest-pinned sandbox policy and real container CI proof are implemented; each target project still
  owns the build/publish pipeline for its exact runtime image/toolchain;
- representative external-repository autonomous acceptance gate is the current release blocker;
- durable registry diagnostics and Prometheus metrics are implemented; OpenTelemetry and optional
  LangSmith tracing may be added without making external tracing the source of evidence;
- multi-project dashboard/operator audit views are optional after the release gate;
- bounded parallel builders only for scheduler-proven non-overlapping write sets.
