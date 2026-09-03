# Durable LangGraph recovery

Converge treats LangGraph checkpoints as the durable source of truth for workflow continuation.
OpenWebUI chat history and OpenCode sessions are never used to determine where a run resumes.

## What happens after a service restart

`ScheduledRunController` scans unfinished runs in the control registry and reads the matching LangGraph
checkpoint from the project's `state_dir/langgraph.sqlite`.

| Checkpoint state | Recovery behavior |
| --- | --- |
| `ci_wait` interrupt | restore the durable `wake_at` timer and resume the same thread when due |
| human risk/TDD/budget interrupt | remain interrupted; operator decision is still required |
| controlled pause | remain paused; never auto-resume |
| `next` node and no interrupt | mark `recoverable` and automatically resume the same LangGraph `thread_id` |
| terminal checkpoint | no recovery action |

A recovered graph is resumed with no new task input. The already checkpointed state remains authoritative.

## Single-writer guarantee during recovery

Every run has a durable SQLite lease with owner, TTL and heartbeat. Before a controller executes or
resumes a run it must claim that lease. If two controller instances discover the same recoverable
checkpoint, only one can execute it. The other retries after a short contention delay. An abandoned
lease expires after the process that owned it stops renewing the heartbeat.

The lease protects workflow execution. It does not grant an LLM or OpenCode process additional Git,
GitHub or filesystem permissions.

## At-least-once side effects

A process can die after an external side effect but before LangGraph commits the next checkpoint.
Therefore recovery assumes that a side-effect node may be executed more than once:

- an existing matching task worktree is adopted rather than force-deleted;
- an already-created candidate commit is recovered and its push may be retried;
- pull-request creation uses `ensure_pull_request()` to reuse an existing task PR;
- merge detects an already merged PR and returns the existing merge SHA;
- CI polling performs one observation per `ci_poll`; waiting is represented by a durable machine
  interrupt rather than a sleeping worker.

These invariants are required for safe automatic restart recovery.

## What recovery must not do

Automatic recovery must never:

- approve a HITL interrupt;
- bypass a deterministic quality, TDD, review, risk or CI gate;
- create a new LangGraph thread for an existing run;
- delete a worktree merely because it is old;
- assume that an expired process lease makes the worktree disposable.

## Remaining crash-hardening work

Stale-resource garbage collection now uses durable ownership records and only removes resources whose
recorded path/branch still match Git and whose owner run is terminal. Active, paused, interrupted,
recoverable and `ci_wait` runs remain protected. Ambiguous or foreign resources fail closed.

The process-level chaos suite now proves two real at-least-once boundaries with separate OS processes:

1. the service is killed after worktree creation and an uncommitted candidate write but before the
   node output checkpoint; restart adopts the exact owned worktree and preserves the candidate with
   no duplicate branch or worktree;
2. the service is killed after candidate commit and remote branch push but before LangGraph can
   checkpoint the node result; restart re-enters the same node, recovers the existing commit, retries
   the push idempotently and proves that neither an extra commit nor a duplicate worktree/branch was
   created.

Remaining end-to-end chaos tests should cover PR creation, CI-wait restoration and explicit
OpenCode/provider process death, then verify that the same run and task converge without losing
candidate changes or duplicating side effects.
