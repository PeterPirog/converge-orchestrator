# External repository acceptance gate

This is the final operational proof before Converge is described as generally ready for unattended,
document-driven repository development within its explicitly supported language scope. It is not a
demo checklist. The release claim requires machine-verifiable evidence from a repository outside
`PeterPirog/converge-orchestrator`.

The acceptance run must preserve the architecture contract: immutable reviewed Markdown remains the
only Source of Truth; LangGraph owns durable workflow state; OpenCode agents remain specialized and
bounded; the Builder is the sole worktree writer; deterministic quality/risk/integration policy cannot
be waived by an LLM; GitHub PR/CI remains the integration gate; and HITL is exceptional.

## Preconditions

Before starting the live run:

1. normalize the source PDF/DOCX/design material into one reviewed Markdown requirements artifact;
2. make that requirements artifact read-only;
3. configure a representative external GitHub repository with meaningful deterministic tests and CI;
4. use a hash-pinned `converge.yaml` run budget and a digest-pinned container sandbox profile;
5. run `converge doctor --config <config>` successfully;
6. provision model-gateway and GitHub credentials through the deployment environment, never in
   requirements, prompts, evidence or repository files.

The target must exercise at least two independently useful requirements so convergence requires at
least two merged task/PR/CI cycles. Do not split one trivial edit into artificial tasks merely to reach
the count.

## Required live scenario

Run the canonical CLI/service controller, not a test-only graph. During the same durable run:

1. allow at least one task to complete through Builder, deterministic gates, all configured independent
   review lanes, PR, authoritative CI and merge;
2. terminate the controller process at a safe but nonterminal point and start a new controller process;
3. verify that the same durable run/thread resumes automatically without manual repository repair or a
   duplicate worktree/branch/PR;
4. deliberately trigger one legitimate exceptional `risk_policy` interrupt, for example a requirement
   that intentionally changes a pre-existing public compatibility contract;
5. resolve only that exceptional condition through the normal decision channel; do not edit code by
   hand;
6. continue until all mandatory requirements are PASS and the run terminates as `converged`.

Routine model/provider/test/CI recovery must remain autonomous inside configured budgets. A test that
requires manual fixes for ordinary failures does not satisfy the release gate.

## Deterministic evidence report

After the run, execute:

```bash
converge-acceptance report \
  --run-id <durable-run-id> \
  --supervisor-evidence <acceptance-supervisor.json>
```

The verifier reads the run's hash-pinned configuration, terminal LangGraph checkpoint, durable resource
budget and evidence bundle. It requires:

- an external GitHub target;
- terminal `converged` status;
- unchanged immutable requirements hash;
- digest-pinned container sandbox;
- a valid finite run-budget ledger;
- at least two distinct merged task cycles;
- complete task/diff/quality/risk/review/PR/CI evidence for each merged task;
- all required deterministic quality gates PASS;
- every configured independent review lane PASS;
- authoritative CI PASS;
- final mandatory compliance PASS;
- supervisor proof of a real controller PID change followed by automatic recovery;
- supervisor proof of one deliberately injected exceptional `risk_policy` HITL decision with no manual
  code edit;
- final independent PASS checks for requirements, architecture, compatibility, security and evidence.

A missing, malformed or ambiguous artifact is FAIL, not PASS. The verifier never calls a model and does
not mutate the target repository.

## Supervisor evidence contract

The external supervisor is deliberately separate from the Converge workflow so a workflow agent cannot
self-certify the release test. Its JSON has this shape:

```json
{
  "version": 1,
  "run_id": "<durable-run-id>",
  "target_repository": "owner/repository",
  "restart": {
    "before_pid": 1234,
    "after_pid": 5678,
    "automatic_recovery_observed": true
  },
  "exceptional_hitl": {
    "kind": "risk_policy",
    "deliberately_injected": true,
    "action": "approve",
    "no_manual_code_edit": true
  },
  "final_independent_checks": {
    "requirements": "pass",
    "architecture": "pass",
    "compatibility": "pass",
    "security": "pass",
    "evidence": "pass"
  }
}
```

The supervisor implementation must observe/record process identity and the decision channel; an operator
must not hand-author a PASS file after the fact. Until that live supervisor and external repository run
exist, `converge-acceptance` correctly reports the release gate as not ready.

## Readiness interpretation

Passing the internal Converge CI suite proves the orchestrator implementation. Passing this external
acceptance gate proves that the integrated product can turn frozen requirements into high-quality
repository changes across repeated autonomous cycles under real GitHub/model/runtime conditions.

Only after both are green should the project be described as generally ready for autonomous repository
development for the declared support scope. Go/Rust or other language claims still require their own
explicit deterministic compatibility scope; the acceptance result must not be generalized beyond what
Converge actually protects.
