# Production persistence

Converge keeps **workflow state outside model/chat sessions**. LangGraph remains the durable workflow
authority; the control registry stores project/run metadata and the run lease used to prevent two
controllers from executing the same thread concurrently.

SQLite remains the zero-configuration default for local development and a single service process.
For production or multiple API workers sharing one deployment, Converge can use PostgreSQL for both:

- LangGraph checkpoints;
- project/run registry and cross-worker run leases.

This changes storage only. It does **not** move planning, retry, merge, risk or HITL decisions into an
LLM or database trigger.

## Install

PostgreSQL support is optional:

```bash
pip install 'converge-orchestrator[postgres]'
```

The extra installs the supported LangGraph PostgreSQL checkpointer and Psycopg driver. SQLite users do
not need these dependencies.

## Configuration

Supply the connection string through the process environment, not `converge.yaml`:

```bash
export CONVERGE_DATABASE_URL='postgresql://converge:...@postgres/converge'
```

`CONVERGE_DATABASE_URL` is service/deployment infrastructure and may contain credentials. Keeping it
outside the project YAML avoids copying database secrets into repositories, generated OpenCode config,
model prompts or evidence.

When this variable is absent, Converge uses the existing SQLite files:

- control registry: `CONVERGE_CONTROL_DB` or `.converge/control.sqlite`;
- checkpoints: `<state_dir>/langgraph.sqlite`.

## First deployment

Run the idempotent schema setup before starting production workers:

```bash
converge persistence-setup
```

The command creates Converge control tables and runs LangGraph `PostgresSaver.setup()` migrations. It
does not print the database URL. Deployment automation should execute this step before rolling out a
new service version.

Converge enforces strict LangGraph msgpack deserialization for the PostgreSQL backend. If
`LANGGRAPH_STRICT_MSGPACK` is unset, Converge sets it to `true`; an explicit unsafe value fails closed.
This reduces the impact of a compromised checkpoint database.

## Runtime behavior

The API and `converge run` automatically select PostgreSQL when `CONVERGE_DATABASE_URL` is present.
Every graph operation opens a PostgreSQL connection configured for LangGraph's synchronous
`PostgresSaver`, then closes it after the operation. The workflow continues to use the same stable
`thread_id`, so recovery semantics do not depend on which service worker executes the next step.

The control registry uses row locking for lease acquisition. A second controller can observe the same
run but cannot claim a non-expired lease owned by another worker. Existing contention recovery remains
machine-managed; it does not create a new HITL path.

Transient PostgreSQL operational/serialization/deadlock failures are classified as retryable during
automatic checkpoint inspection. Ambiguous or non-transient persistence failures remain fail-closed.

## Scope and current deployment boundary

PostgreSQL removes the process-local database constraint, but it does not make the whole Converge
runtime stateless. The following are still filesystem resources under the project/state directories:

- target repository and Git worktrees;
- evidence bundles and compliance files;
- generated OpenCode runtime configuration and managed Skills.

Therefore:

- multiple workers on one host/shared filesystem can coordinate through the shared registry and run
  lease;
- multiple independent nodes require a deliberately shared/consistent project filesystem or workload
  affinity until evidence/workspace storage is externalized.

Do not run workers on independent local clones and assume PostgreSQL alone makes their worktrees
interchangeable.

## Deployment backup

A production backup must cover **both** durable database state and filesystem-backed project state.
Converge provides a deployment-wide, fail-closed backup command:

```bash
converge-backup create /srv/backups/converge-2026-09-04
```

The command uses the same persistence selection as the runtime:

- without `CONVERGE_DATABASE_URL`, the SQLite control registry and each project's LangGraph SQLite
  checkpoint database are copied using SQLite's online backup API;
- with `CONVERGE_DATABASE_URL`, PostgreSQL is captured with `pg_dump --format=custom`; the database URL
  is passed through `PGDATABASE`, not command-line arguments;
- every registered repository is captured as a Git bundle;
- project configuration, immutable requirements and non-transient state/evidence files are copied;
- raw worktree directories and raw SQLite WAL/SHM files are not copied.

### Quiescence requirement

Backup creation is intentionally stricter than normal read-only diagnostics. It is rejected if any
registered project has:

- an unfinished durable run;
- an active or pending-cleanup Converge worktree ownership record;
- uncommitted changes in the canonical repository;
- invalid project configuration or workspace/state-store affinity.

The registry and project fingerprints are checked before and after capture. If configuration,
requirements, repository HEAD or durable registry state changes during the backup, the staging tree is
deleted and no backup is published. This avoids creating a snapshot that looks valid but combines two
different execution states.

### Integrity verification

Verify a backup before copying it off-host and again before restore preflight:

```bash
converge-backup verify /srv/backups/converge-2026-09-04
```

Verification is offline: it does not open the active control database or contact models, GitHub,
OpenWebUI or OpenCode. It validates the versioned manifest, exact file set, file sizes and SHA-256
hashes and rejects symlinks, unexpected files, missing files or modified content.

The command prints only a bounded operational summary: backup path, creation timestamp, persistence
backend, project count and file count. It does not print database credentials or backup file contents.
The manifest/hash mechanism provides deterministic corruption and consistency detection; it is not a
cryptographic signature against an attacker who can rewrite both payload and manifest.

### PostgreSQL prerequisite

`pg_dump` must be installed on the worker that creates a PostgreSQL-backed backup and should match the
major PostgreSQL server version according to normal PostgreSQL operational practice. A missing or
failing `pg_dump` aborts the backup.

## Restore preflight

Restore is deliberately split into a read-only planning phase and a later explicit apply phase. Run:

```bash
converge-backup restore-plan /srv/backups/converge-2026-09-04
```

`restore-plan` first repeats backup integrity verification and then validates the current restore host
without writing deployment state. It checks, among other invariants:

- the declared SQLite/PostgreSQL backend matches the actual database artifact in the backup;
- the current runtime backend selection matches the backup;
- the SQLite control database target is absent, or for PostgreSQL the configured target contains no
  user relations and `pg_restore` is available;
- each manifest project ID and workspace/state-store identity is valid;
- configuration and immutable-requirements hashes still match project metadata in the manifest;
- the Git bundle contains the exact repository HEAD recorded by the manifest;
- the backed-up state-store identity marker matches the manifest;
- configuration, requirements, repository, state and worktree destination paths are absolute and do
  not already exist, including broken symlinks;
- repository/state directory targets from different projects do not overlap.

A blocked plan is printed as structured evidence and exits non-zero. A ready plan contains a
`confirmation_token`, which is a SHA-256 binding of the backup manifest digest and the exact sanitized
restore plan. It is intended for the later apply command to require an explicit operator decision tied
to that exact preflight result. Changing the backup or target state must require a new plan/token.

The plan never prints `CONVERGE_DATABASE_URL`. For PostgreSQL it reports only a sanitized configured or
unconfigured target classification.

### Current restore status

`restore-plan` is implemented; destructive restore **apply is not implemented yet**. There is no
`--force` path and the preflight itself does not create repositories, state directories or databases.
Do not overwrite a live deployment manually based only on a ready plan.

The apply phase is intentionally separate because restoration changes durable control/checkpoint state
and filesystem identity. It must recompute preflight immediately before writing, require the exact
operator-provided confirmation token, refuse existing targets, and use transactional/staged publication
where the backend allows it. Until that phase is implemented and tested, `create`, `verify` and
`restore-plan` are data-protection and recovery-planning primitives rather than a complete disaster
recovery workflow.

## Verification

CI contains a focused PostgreSQL integration job, separate from the Python 3.11-3.13 unit matrix. It
proves that two registry instances observe the same project/run data, lease acquisition is mutually
exclusive across instances, and a LangGraph checkpoint survives closing one database connection and
opening another.

Backup tests use real Git and SQLite and cover successful capture, Git bundle validity, checkpoint
integrity, tamper detection, unfinished-run blocking, worktree ownership blocking, dirty-repository
blocking, symlink rejection and secret-free PostgreSQL command arguments. Restore-preflight tests build
a real backup, simulate loss of the original deployment, validate exact empty restore targets, reject
broken symlinks and mismatched Git HEAD/backend artifacts, and prove that PostgreSQL credentials are not
included in the serialized plan.
