# Convergence audit against the autonomous-agent reference architecture

This document is the living gap analysis for Converge. It compares the repository implementation with
the reference requirements for a reusable autonomous software-engineering orchestrator: immutable
intent, specialized roles, bounded tasks, TDD where appropriate, independent review, Git/GitHub gates,
minimal HITL, durable LangGraph execution and simple project reconfiguration.

The audit tracks architectural intent rather than copying every technology choice from the reference
document. LangGraph owns durable workflow state and routing; OpenCode is the repository-aware coding
runtime; OpenWebUI is an operator/model control plane; deterministic Python policy and GitHub CI remain
authoritative for integration.

## Current status

The core autonomous path is substantially converged with the reference design. Fourteen of the fifteen
tracked areas are aligned or stronger than the reference. `MCP as the universal tool bus` remains
partial deliberately: agent-facing context/tools can use MCP, while Git, GitHub, test and integration
decisions that affect safety remain deterministic code where that provides a smaller and more
verifiable trust boundary.

The earlier high-priority recovery gaps are now closed with executable evidence:

- durable run leases prevent two controllers from executing one LangGraph thread concurrently;
- worktree creation, commit/push, PR creation and machine-managed CI wait have process-level
  kill/restart coverage;
- OpenCode/executor process death is covered by a real subprocess test and bounded provider retry;
- CI retry is allowed only for explicitly configured exact flaky GitHub Actions jobs and is protected
  by a durable per-head retry ledger;
- SQLite is retained for local use, while PostgreSQL provides shared control-registry and LangGraph
  checkpoint persistence with real-database CI coverage;
- each new run owns a hash-pinned normalized configuration snapshot, so changing `converge.yaml`
  cannot silently change model, gate, retry/replan, CI or merge policy in a run already in progress;
- CLI recovery resolves an existing durable run from control-registry identity before reading or
  re-registering mutable source YAML, preserving the same pinned execution policy after restart;
- durable low-cardinality diagnostics/Prometheus metrics are reconstructed from shared registry state;
- an authenticated workload-affinity probe lets an external scheduler identify which worker can safely
  execute a filesystem-bound project, using the active run's pinned configuration when one exists;
- globally quiesced deployment backup plus deterministic restore preflight protect database, repository,
  immutable requirements and filesystem-backed state together;
- SQLite deployments have crash-resumable restore apply with database-last publication, exact target
  validation and a durable completion receipt that closes the final process-response race;
- PostgreSQL deployments have crash-safe restore apply with an exact plan-bound server receipt committed
  atomically with the restored database, allowing deterministic recovery after database commit but
  before local journal acknowledgement without a second restore.

This does **not** mean production hardening is finished. Both SQLite and PostgreSQL deployments now have
a complete create/verify/plan/apply recovery path for the backed-up artifacts. Independent multi-node
deployments still require a shared/consistent project filesystem or deliberate external artifact
storage, and deterministic source-level compatibility policy remains materially stronger for Python
than for Node/Go/Rust.

## Convergence matrix

| Area | Status | Current Converge implementation | Remaining gap |
| --- | --- | --- | --- |
| Immutable Markdown Source of Truth | **STRONGER** | architecture file outside target repo, OS read-only policy, SHA-256 pin, repeated hash guards, traceable `contract.json` | no critical gap |
| Architectural-drift prevention | **STRONGER** | stable requirement IDs/source anchors, exact requirement injection, compliance, deterministic verifiers, monotonic mandatory-regression policy, per-run execution-policy pinning | semantic-only requirements still require independent review |
| Deterministic controller above LLMs | **STRONGER** | LangGraph + Pydantic state + deterministic policy; model output cannot waive gates or authorize merge | no critical gap |
| Planner / Worker / Reviewer separation | **STRONGER** | Scout RO, Planner RO, Builder sole worktree writer, independent correctness/architecture/security reviewers RO, deterministic Integrator | specialty analyzers remain optional extensions |
| Autonomous TDD / repair loop | **ALIGNED** | behavior tasks use baseline, test-only RED, frozen test hashes and GREEN; bounded repair and replan; no human bypass of deterministic failures | language-specific `change_kind` inference can be stronger |
| Git isolation | **STRONGER** | one deterministic worktree per task, crash-safe adoption, ownership-aware cleanup, active/recoverable/CI-wait resource protection, explicit worker-affinity probe | multi-node shared/external workspace durability remains deployment work |
| Independent review barrier | **STRONGER** | deterministic risk scan before semantic review, three independent lanes, one reject/execution failure blocks integration, secret material blocked before reviewer exposure | additional specialty lanes are optional |
| GitHub PR + CI | **STRONGER** | retry-safe push/PR/merge, origin validation, classic protection + effective Rulesets checks, App-ID-aware matching, checkpointable CI wait, explicit bounded flaky-job retry | GitHub remains final enforcement point for policies Converge intentionally does not duplicate |
| MCP as universal tool bus | **PARTIAL BY DESIGN** | role-scoped MCP configuration generated for OpenCode | critical Git/GitHub/test/integration authority intentionally stays deterministic rather than MCP-only |
| OpenWebUI operator entry point | **ALIGNED** | confirmed Workspace Tool over Bearer-authenticated FastAPI; status/compliance/evidence/interrupt operations; durable state outside chat | richer dashboard is optional UX |
| Reusable project configuration | **STRONGER** | one `converge.yaml`, relative paths, model profiles, MCP, sandbox, quality/workflow policy, per-run normalized immutable execution snapshot | GUI editor is optional |
| Minimal HITL | **STRONGER** | HITL only for explicit risk/ambiguity or exhausted bounded recovery; routine provider failures, CI waits and recoverable crashes resume automatically | additional deterministic compatibility policy can further reduce valid escalations |
| Least privilege / sandbox | **STRONGER** | protected role permissions, Builder-only write, RO Git metadata, container root RO, cap-drop, no-new-privileges, resource/network/env limits and timeout cleanup | pinned production images and deployment hardening |
| Context rotation / bounded memory | **ALIGNED** | fresh OpenCode sessions, LangGraph/evidence continuity, authoritative core never silently truncated, advisory compaction, bounded fallback attempts | provider token/cost telemetry |
| Evidence + durable compliance | **STRONGER** | event/evidence bundles, persistent compliance, verifier/TDD/risk/CI evidence, SQLite or PostgreSQL durable workflow state, durable registry diagnostics, coordinated backup and crash-safe restore for both persistence backends | optional shared/external artifact storage |

## Canonical execution path

The service and CLI use the same `ScheduledRunController`; there is no separate weaker CLI graph path.
The durable service graph remains:

```text
bootstrap -> spec guard -> scout -> targeted planner -> worktree
                                      |
                                      v
                           TDD baseline / RED / build
                                      |
                                      v
                       scope + quality + risk policy
                                      |
                                      v
                    parallel independent RO reviews
                                      |
                                      v
                         bounded repair / replan
                                      |
                                      v
                         integrate -> PR -> ci_poll
                                           |
                                     pending v
                                      ci_wait interrupt
                                           |
                                     machine resume
                                           v
                                        ci_poll
                                           |
                                 PASS -> merge -> refresh
                                           |
                                  next target / converged
```

`ci_wait` is a machine interrupt, not HITL. Its wake time is checkpointed, the worker and lease are
released while idle, and a new controller reconstructs the timer after restart.

## Immutable intent versus pinned execution policy

Converge now protects two distinct classes of drift and does not conflate them:

1. `architecture.md` is the immutable **Source of Truth**. Its SHA-256 and source anchors protect what
   the repository is required to become. `contract.json` remains an index, never a replacement.
2. `converge.yaml` is the user-maintained **Source of Configuration for future runs**. At run creation,
   Converge normalizes it into `state_dir/run-configs/`, embeds the SHA-256 in the snapshot identity and
   stores path + digest in the durable run record.

Every graph open, status/resume path and automatic recovery of a new run verifies and reloads that exact
snapshot. A missing, incomplete or tampered snapshot fails closed. Recovery never silently falls back
to the current project YAML. Legacy run rows retain their historical behavior only for compatibility.

This distinction is important: freezing execution policy prevents a long-running task from changing
models, quality gates or merge semantics mid-flight, but it does not elevate configuration into a
second requirements source.

## Crash recovery and at-least-once semantics

LangGraph may re-enter a node whose external side effect completed immediately before a lost checkpoint.
Converge therefore treats side-effect nodes as retry-safe `ensure` operations rather than blind creates:

- task worktree creation adopts only the expected owned path/branch;
- integration recovers an already-created candidate commit and can retry push idempotently;
- PR creation reuses an existing matching PR;
- merge recognizes an already-merged PR;
- terminal LangGraph checkpoints reconcile stale control-registry rows without re-execution;
- pre-first-node recovery reconstructs only the exact minimal initial envelope and, for pinned runs,
  replays the same snapshot path;
- checkpoint corruption is never interpreted as an empty run;
- transient checkpoint lock/busy errors schedule bounded automatic inspection retry.

The same rule applies to deployment disaster recovery outside the autonomous graph. SQLite restore uses
exact staged publications and a durable completion receipt. PostgreSQL restore appends a plan-bound
receipt to the materialized restore script and commits that receipt in the same server transaction as
the restored database. If the process dies after the server commit but before the local journal records
`database`, the next invocation validates the exact receipt and adopts the completed database instead
of issuing an ambiguous second restore. Both backends keep the control/database publication last and
require a fresh ready preflight before a completed receipt can be reused after later total loss.

Process-level tests cover the worktree, commit/push, PR and `ci_wait` boundaries. The executor suite also
proves fresh-process retry after abrupt OpenCode process death. PostgreSQL restore has a real subprocess
proof using abrupt process termination after the committed server receipt and before local journal
acknowledgement. New chaos fixtures should be added only when a genuinely uncovered side-effect boundary
is discovered.

## GitHub remote policy

Before a GitHub-backed project performs remote side effects, local `origin` must match the configured
canonical `owner/repo`. Required-check policy is constructed from classic branch protection plus active
GitHub Rulesets for the actual base branch. Where a Ruleset binds a check to an integration, matching
uses the GitHub App ID as well as the context name.

Unknown, malformed or unreadable protected-branch policy fails closed. Explicit flaky retry is narrower:
only exact configured GitHub Actions checks may be rerun, only within their retry budget, and the budget
reservation is durable before the remote rerun request. Mixed or unclassified failures remain failures.

## Context, review and sandbox boundaries

Each OpenCode agent attempt starts with a fresh session. Continuity lives in LangGraph state, bounded
repo/task context and evidence rather than accumulated chat history. Requirement statements, Task
Envelope and review diff are authoritative context and cannot be silently truncated to fit a model.

Semantic review is parallel only for read-only lanes. Builder remains the sole code writer in its
worktree. Model/provider failure can trigger only configured bounded retries/fallbacks with unchanged
permissions and a durable attempt ledger. Malformed semantic output is not converted into a provider
success.

`ExecutionSandbox` covers OpenCode, quality gates and requirement verifiers. Deterministic integration
stays outside the Builder authority. Container mode enforces read-only root, dropped capabilities,
no-new-privileges, resource limits, tmpfs, controlled environment forwarding, network policy and
cleanup on timeout.

## Multi-node placement boundary

The shared PostgreSQL registry/checkpointer does not imply that arbitrary workers own every project
filesystem. Converge preserves the existing workspace/state-store binding as the deterministic execution
guard and exposes a read-only authenticated affinity probe for deployment routing.

For an unfinished run, the probe validates worker eligibility against the run's pinned configuration;
it does not consult current mutable YAML. With no unfinished run it validates current project
configuration because that is the policy a future run would start from. Missing storage, mismatch,
invalid configuration or ambiguous unfinished runs produce bounded fail-closed classifications without
raw paths or exception text.

This endpoint is advisory to the external scheduler but not advisory to Converge's safety checks: a
worker selected incorrectly still fails the controller's workspace/state-store assertions. The probe
does not migrate workspaces, rewrite registry bindings or become a workflow-state authority.

## Current next priorities

Repository evidence now moves the priority away from completed checkpoint/flake/PostgreSQL persistence,
metrics, workload-placement and deployment backup/restore recovery work. The smallest remaining
high-value areas are, in order:

1. **Cross-language deterministic compatibility/architecture adapters** — source-level Node first,
   then Go/Rust public API and dependency-boundary rules, extending the current Python AST and Node
   manifest policy. These gates should reduce semantic-review ambiguity and therefore avoid otherwise
   valid HITL escalations without giving models policy authority.
2. **Cost/time governance** — bounded run/project budgets and provider-reported usage telemetry after
   the core operational durability path is hardened.
3. **Deployment portability hardening** — pinned production sandbox images and deliberately
   shared/external artifact storage where independent multi-node workers require it.

Optional issue synchronization, richer dashboards and broader UX must not displace these core items.
Parallel Builders should remain disabled until a deterministic scheduler can prove non-overlapping write
sets and revalidate integration against the current main branch.

## Target operational criterion

Converge is operationally converged with the reference vision when all of the following remain true:

- immutable requirements cannot be changed or displaced by derived summaries;
- every task is bounded and has one writer;
- behavior-changing tasks prove RED before GREEN when TDD is applicable;
- deterministic gates and every configured independent review lane pass before integration;
- GitHub required CI policy validates the exact candidate commit;
- crash recovery neither loses nor duplicates externally visible side effects;
- one durable run cannot change execution policy because mutable project configuration changed;
- filesystem-bound project operations are routed only to workers that satisfy deterministic affinity;
- routine failures repair/replan/retry autonomously within explicit budgets before HITL;
- OpenWebUI controls the process without becoming durable workflow storage;
- sandbox and role permissions bound blast radius independently of model behavior;
- context remains bounded during long-running projects;
- cleanup never removes resources belonging to active, recoverable, interrupted or CI-wait runs.
