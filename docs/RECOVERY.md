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
| no first checkpoint yet | reconstruct only the original minimal run input and restart the same thread |
| terminal checkpoint | no recovery action |

A recovered graph is resumed with no new task input once a checkpoint exists. If the service dies before
the first LangGraph checkpoint is durable, the control registry still contains the immutable run/thread
identity and project configuration reference. Converge reconstructs only that original minimal input and
re-enters the same thread; it does not invent a task or reuse chat/model history.

Checkpoint inspection errors are deliberately different from an absent first checkpoint. A database or
configuration read failure is recorded and fails closed; it is never treated as permission to restart
from an empty state.

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

These invariants are required for safe automatic restart recovery, including the pre-first-checkpoint
window where the first node may be re-entered after process death.

## What recovery must not do

Automatic recovery must never:

- approve a HITL interrupt;
- bypass a deterministic quality, TDD, review, risk or CI gate;
- create a new LangGraph thread for an existing run;
- delete a worktree merely because it is old;
- assume that an expired process lease makes the worktree disposable;
- confuse unreadable/corrupt checkpoint storage with a legitimately absent first checkpoint.

## Process-level crash coverage

Stale-resource garbage collection uses durable ownership records and only removes resources whose
recorded path/branch still match Git and whose owner run is terminal. Active, paused, interrupted,
recoverable and `ci_wait` runs remain protected. Ambiguous or foreign resources fail closed.

The process-level chaos suite exercises four critical workflow recovery boundaries with separate OS
processes:

1. **Worktree creation** — the service is killed after worktree creation and an uncommitted candidate
   write but before the node output checkpoint. Restart adopts the exact owned worktree, preserves the
   candidate and creates no duplicate branch or worktree. Recovery also handles the narrower race in
   which the service dies before even the first LangGraph checkpoint becomes durable.
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

The executor resilience suite also crosses the real OS subprocess boundary: a local fake OpenCode
process terminates abruptly on its first invocation, and the configured bounded primary retry launches
a fresh process with the exact same role, prompt and model. The retry succeeds without hidden session
continuation or HITL, and the provider-health ledger records only bounded attempt metadata.

Additional chaos fixtures should be added only for newly discovered failure boundaries that are not
already covered by deterministic retry/fallback, LangGraph recovery, or retry-safe side-effect tests.
