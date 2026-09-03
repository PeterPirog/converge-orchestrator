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

## Process-level crash coverage

Stale-resource garbage collection uses durable ownership records and only removes resources whose
recorded path/branch still match Git and whose owner run is terminal. Active, paused, interrupted,
recoverable and `ci_wait` runs remain protected. Ambiguous or foreign resources fail closed.

The process-level chaos suite now exercises four critical recovery boundaries with separate OS
processes:

1. **Worktree creation** — the service is killed after worktree creation and an uncommitted candidate
   write but before the node output checkpoint. Restart adopts the exact owned worktree, preserves the
   candidate and creates no duplicate branch or worktree.
2. **Commit and push** — the service is killed after candidate commit and remote branch push but before
   LangGraph checkpoints the node result. Restart recovers the exact commit and retries the push
   idempotently without another commit, worktree or task branch.
3. **Pull request creation** — the service is killed after the external PR exists but before the PR
   node result is checkpointed. Restart re-enters the same node and `ensure` semantics reuse the exact
   existing PR instead of creating a duplicate.
4. **Machine-managed CI wait** — the service is killed after a durable `ci_wait` interrupt has released
   the worker and lease while its wake timer exists only in process memory. A fresh controller restores
   the original `wake_at`, automatically resumes the same run/thread and reaches the next node without
   a human `/resume` or `/decision` call.

Remaining end-to-end chaos work should focus on explicit OpenCode/provider process death and any newly
discovered external side-effect boundary that lacks an equivalent retry-safe process-level proof.
