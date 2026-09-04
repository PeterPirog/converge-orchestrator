# Run resource budgets

Converge gives every durable autonomous run a finite resource envelope. The envelope is deterministic
controller policy: Planner, Builder and Reviewers may consume it but cannot enlarge, reset or waive it.
Budget exhaustion is therefore not a human-approval path and does not weaken tests, review or GitHub CI.

## Configuration

`workflow.run_budget` is normalized into the same SHA-256-pinned run configuration snapshot as the rest
of execution policy:

```yaml
workflow:
  run_budget:
    max_wall_time_seconds: 86400
    max_model_attempts: 512
    max_estimated_tokens: 8000000
```

The defaults are deliberately finite. Projects should lower them after measuring representative runs.
Changing the source `converge.yaml` affects future runs only; a running or recovered workflow continues
with the exact budget that was pinned when its durable run record was created.

## Durable accounting

Each run owns `state_dir/evidence/<run-id>/run-budget.json`. The ledger records the original durable run
start time plus reserved model attempts and estimated model tokens. It is atomically replaced and
fsync'ed before a provider process is launched.

A reservation includes the deterministic input estimate produced by the existing context guard plus the
configured output reserve. This is intentionally conservative and provider-independent; it is a safety
limit, not a claim about billable tokens. Provider-reported usage is recorded separately for economic
reporting and never becomes the authority that decides whether another model call is allowed.

Reservations are at-most-once grants and at-least-once charges: if a process dies after the reservation
but before the provider response is durably observed, the reservation remains consumed. Retries and
fallback models therefore cannot obtain a fresh resource envelope after a crash. The active run lease
prevents two controllers from reserving resources for the same LangGraph thread concurrently, while the
parallel read-only review lanes serialize updates to the shared run ledger.

## Wall-time boundary

The controller checks the wall-time deadline before starting or resuming graph execution. Model calls
also reserve only the remaining full seconds of the run deadline, and the OpenCode process timeout is
reduced to the smaller of the role timeout and that remaining budget. A provider invocation therefore
cannot intentionally start with a timeout extending beyond the pinned run envelope.

An exact model-attempt or estimated-token limit does not cancel deterministic work that is already able
to finish without another model call, such as integration or CI handling. The next model reservation is
refused instead. Wall-time expiry at a later controller resume is terminal because the run itself has
exhausted its time envelope.

## Failure semantics

- the next reservation that would exceed a model-attempt or estimated-token limit is rejected before
  provider execution;
- expired wall time is rejected before controller submission/resume and before a new provider call;
- a missing ledger is created only for a newly queued run, using the durable registry `started_at`,
  which closes the process-crash window between run creation and first execution;
- a missing or corrupt ledger for an already active/recoverable run fails closed rather than resetting
  unknown historical consumption;
- resource exhaustion becomes terminal `budget_exhausted`; it is not converted to repair/replan or HITL;
- deterministic quality, compatibility, security and GitHub CI gates remain mandatory regardless of
  remaining budget.

This policy bounds autonomous resource consumption without making cost optimization more authoritative
than correctness or immutable requirements.

## Measured provider usage

Converge requests stable OpenCode JSON events and reads each completed `step_finish` event's reported
input, output, reasoning and cache token counters plus USD cost. The original agent text is reconstructed
from completed text events before Planner/Builder/Reviewer schema handling, so telemetry does not become
agent output or policy input.

Every provider invocation that already owns a run-budget reservation writes one atomic artifact under
`state_dir/evidence/<run-id>/model-usage/attempt-<reservation>.json`. Binding the measurement to the
pre-execution reservation makes retries, fallback models and parallel review lanes auditable without
inventing a second attempt identity. Records include unavailable/invalid coverage states and never infer
missing values as zero-cost provider usage.

`GET /runs/<run-id>/model-usage` returns deterministic totals and breakdowns by role and resolved model,
including a `coverage_complete` flag. The measured ledger is observational: a write failure is recorded
in invocation/provider-health context but does not retry an otherwise successful paid model call. The
conservative reservation ledger remains the fail-closed execution boundary regardless of reported cost.
