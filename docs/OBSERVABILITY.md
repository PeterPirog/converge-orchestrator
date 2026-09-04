# Production observability

Converge exposes a deliberately small operational view that is reconstructed from the **durable
control registry**. Observability is read-only: it does not become a LangGraph state authority and it
cannot change routing, retries, HITL decisions, quality gates, CI policy or merge behavior.

## Endpoints

The FastAPI control plane exposes:

- `GET /health` — liveness only. This remains public when API Bearer authentication is enabled.
- `GET /diagnostics` — JSON operational snapshot.
- `GET /metrics` — Prometheus text exposition (`text/plain; version=0.0.4`).
- `GET /projects/{project_id}/affinity` — authenticated worker-placement probe for multi-node routing.

`/diagnostics`, `/metrics` and the affinity probe use the same Bearer authentication as the rest of the
control plane. Do not expose them publicly without the same network and authentication controls used
for the operator API.

Example:

```bash
curl \
  -H "Authorization: Bearer $CONVERGE_API_TOKEN" \
  http://127.0.0.1:8088/diagnostics

curl \
  -H "Authorization: Bearer $CONVERGE_API_TOKEN" \
  http://127.0.0.1:8088/metrics
```

A Prometheus scrape job can use the normal `authorization.credentials_file` or another deployment
secret mechanism. Do not put the Bearer token into `converge.yaml`.

## Durable rather than process-local

The snapshot is derived from the configured control registry. In local mode this is SQLite; in shared
production mode it is PostgreSQL. Metrics therefore do not reset merely because an API worker restarts,
and multiple workers reading the same PostgreSQL registry observe the same durable run metadata.

The collector intentionally does **not** inspect:

- prompts or model responses;
- LangGraph checkpoint payloads;
- Git repositories or worktrees;
- evidence bundles;
- OpenWebUI or provider APIs.

This keeps a scrape cheap relative to an autonomous run and prevents monitoring from acquiring model,
Git or integration authority.

## Signals

The current snapshot reports low-cardinality operational health:

- selected persistence backend;
- registered project count;
- project workspace/state-store affinity health;
- total and unfinished run counts;
- run counts grouped by durable status;
- count of runs with a durable error marker;
- active, expired, missing or malformed run-lease state;
- pinned, legacy-unpinned or incomplete per-run configuration snapshot state;
- age of the oldest unfinished run;
- malformed durable timestamp count.

The Prometheus representation does not use project IDs, run IDs, thread IDs, task IDs, workspace IDs,
controller IDs, paths or error messages as labels. This prevents unbounded label cardinality and avoids
turning monitoring into a side channel for project content.

## Suggested alerts

Alert thresholds are deployment-specific, but useful conditions include:

- `converge_registry_timestamp_parse_issues > 0`;
- `converge_run_config_snapshots{state="incomplete"} > 0`;
- `converge_project_affinity{state="incomplete"} > 0`;
- unexpected growth of `converge_run_leases{state="expired"}`;
- `converge_oldest_unfinished_age_seconds` exceeding the expected maximum project/run duration;
- a persistent increase in `converge_runs_with_error`.

These are diagnostic signals, not automatic permission to mutate or terminate a run. Recovery and
routing remain controlled by the durable LangGraph/controller policy.

## Multi-node workload-affinity contract

PostgreSQL plus durable metrics does not make the whole executor stateless. Candidate repositories,
worktrees, evidence and generated role runtime artifacts remain filesystem-backed. Independent nodes
therefore still require either:

1. a deliberately shared/consistent workspace and state filesystem, or
2. workload affinity that routes a registered project to a worker that owns its bound workspace and
   state store.

The authenticated `GET /projects/{project_id}/affinity` endpoint is the deployable contract for option
2. A scheduler or gateway can query candidate workers before sending project/run operations and choose
an eligible worker. The response contains only bounded placement metadata:

```json
{
  "project_id": "payments",
  "eligible": true,
  "basis": "pinned_run",
  "reason": "local",
  "unfinished_runs": 1
}
```

The probe follows durable ownership rules rather than mutable convenience:

- if exactly one unfinished run exists, placement is validated from that run's hash-pinned
  configuration snapshot (`basis=pinned_run`); current `converge.yaml` is not consulted;
- if no unfinished run exists, placement is validated from current project configuration because that
  is what a future run would use (`basis=project_config`);
- multiple unfinished runs are treated as ambiguous and return `eligible=false` rather than guessing;
- missing local storage, invalid configuration and workspace/state-store mismatch return stable,
  sanitized reason codes without filesystem paths or raw exception text.

A scheduler must fail closed when no worker reports `eligible=true`. More than one eligible worker is
valid only when those workers deliberately see the same bound workspace/state filesystem; the durable
run lease still prevents concurrent execution of one LangGraph thread. `/health` is only liveness and
must never be used as a substitute for project affinity.

The affinity probe does not migrate data, rewrite bindings or acquire execution authority. Existing
workspace/state-store checks remain the final deterministic guard inside the controller. External/shared
artifact storage and backup/restore automation remain separate production-hardening work.

## Privacy and retention

Prometheus output is aggregate metadata only. `/diagnostics` likewise omits raw error text, paths and
individual run identifiers. The project affinity endpoint is authenticated and returns only bounded
placement classifications. Detailed evidence remains in the normal authenticated evidence API and
filesystem store, where its project-specific retention policy can be managed separately.
