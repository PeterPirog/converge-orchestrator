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

Restore is deliberately split into a read-only planning phase and an explicit apply phase. Run:

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
restore plan. SQLite binds the exact normalized database path. PostgreSQL additionally contributes a
secret-free digest of the configured connection target, so changing the configured target database
invalidates an earlier token without serializing its password or connection URL.

The plan never prints `CONVERGE_DATABASE_URL`. For PostgreSQL it reports only a sanitized configured or
unconfigured target classification.

## SQLite restore apply

SQLite deployments have an explicit write-capable restore command:

```bash
converge-backup restore-apply /srv/backups/converge-2026-09-04 \
  --confirmation-token <token-from-restore-plan>
```

This command is intentionally an **operator action**, not a LangGraph node and not an agent tool. On a
new restore attempt it repeats backup verification and restore preflight immediately before the first
write and requires the exact token from that ready preflight. There is no `--force` option.

The restore uses staged publication and restores the durable control registry **last**. Before the
control database becomes visible it restores and verifies:

1. immutable requirements and project configuration with their manifest hashes;
2. each Git repository at the exact manifest HEAD, with a clean working tree, the original Converge
   workspace identity and a canonical GitHub `origin` when `github.repo` is configured;
3. the exact state/evidence file set, state-store identity and LangGraph SQLite checkpoint;
4. an empty worktree location rather than stale task worktrees.

SQLite database artifacts are checked with `PRAGMA quick_check`. Existing targets are never overwritten.
A matching target is accepted only during recovery of a journaled restore and only after deterministic
content/identity validation.

### Process-crash recovery

Because configuration, repositories and state may live on different filesystems, Converge does not
claim a globally atomic filesystem transaction. Instead it keeps a local mode-0600 restore journal and
uses retry-safe `ensure` semantics around every publication boundary. If a process dies after an exact
target was published but before the journal checkpoint, rerunning the same command with the **same
confirmation token** validates and adopts that exact target and continues.

The fully published journal is retained as a completion receipt instead of being deleted immediately
before returning success. This closes the final process-crash window: if the process dies after the
last database publication or integrity check but before the operator receives the response, the same
command and token deterministically validate the restored deployment and return success again rather
than creating an avoidable HITL/manual-cleanup path.

A completed receipt does not permanently consume the backup. If a later disaster removes every restore
target, ordinary read-only preflight becomes ready again. Only when that fresh preflight reproduces the
same confirmation token may Converge atomically reset the receipt and start a new restore cycle. Partial
loss, changed targets or a changed token remain fail-closed.

A target recorded as published that later disappears, becomes dirty or changes content is not silently
re-created during recovery of an incomplete cycle. The stored plan is itself re-bound to the original
confirmation token before a resumed write. The control registry database is published last, so a
failure during earlier filesystem restoration does not expose the partial deployment through normal
Converge discovery.

This recovery contract is for process interruption. It is not a claim of power-loss atomicity across
independent filesystems; deployment-level storage durability and filesystem guarantees still apply.

## Current PostgreSQL restore status

PostgreSQL **restore apply is not implemented yet**. `restore-plan` supports PostgreSQL and binds the
confirmation token to the configured target, but `restore-apply` rejects a PostgreSQL backup before
writing anything. A production PostgreSQL apply must preserve the same no-force contract and define a
safe, tested commit/recovery boundary around `pg_restore --single-transaction --exit-on-error` before
it is enabled.

Do not manually overwrite a live PostgreSQL deployment based only on a ready plan.

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
included in the serialized plan. SQLite restore-apply tests prove complete reconstruction, storage
identity preservation, wrong-token no-write behavior, database-last publication, recovery across the
publication/journal checkpoint race, idempotent completion-receipt replay and safe reuse after a later
full target loss.
