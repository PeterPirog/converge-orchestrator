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

## Backup

A production backup must cover **both** the PostgreSQL database and the project/state filesystem. A
PostgreSQL-only backup restores durable graph/control metadata but not candidate worktrees or evidence.
Backup/restore automation and externalized evidence storage remain production-hardening work.

## Verification

CI contains a focused PostgreSQL integration job, separate from the Python 3.11-3.13 unit matrix. It
proves that two registry instances observe the same project/run data, lease acquisition is mutually
exclusive across instances, and a LangGraph checkpoint survives closing one database connection and
opening another.
