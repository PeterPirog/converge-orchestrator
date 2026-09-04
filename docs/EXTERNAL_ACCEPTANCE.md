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
4. configure the acceptance base branch with at least one authoritative required GitHub status check
   through branch protection or an effective Ruleset; a workflow that merely happens to run is not an
   integration gate;
5. use a hash-pinned `converge.yaml` run budget and a digest-pinned container sandbox profile;
6. run `converge doctor --config <config>` successfully;
7. provision model-gateway and GitHub credentials through the deployment environment, never in
   requirements, prompts, evidence or repository files;
8. configure `auto_merge: true` and the required correctness, architecture and security review lanes.

The target must exercise at least two independently useful mandatory requirements so convergence
requires at least two merged task/PR/CI cycles. Do not split one trivial edit into artificial tasks
merely to reach the count.

## Canonical live supervisor

Use the real supervisor command rather than manually assembling acceptance evidence:

```bash
converge-acceptance supervise \
  --config /absolute/path/to/converge.yaml \
  --project-id external-acceptance \
  --expected-risk-flag forbidden_public_api_change \
  --output /absolute/path/to/acceptance-supervisor.json
```

The `expected-risk-flag` must be planned into the acceptance target as one legitimate exceptional
condition. The supervisor does not create that condition itself and never edits target code.

The supervisor launches the normal authenticated FastAPI/LangGraph controller as a child process and
uses only the public control-plane API for project registration, run start/status and the deliberate
risk decision. It remains outside LangGraph so it can independently prove controller-process failure
and recovery without becoming workflow policy.

## Required live scenario

During the same durable run the supervisor must prove all of the following:

1. at least one task completes through Builder, deterministic gates, all configured independent review
   lanes, PR, authoritative required CI and merge;
2. the supervisor terminates that controller process at a nonterminal point and starts a new controller
   process with a different PID;
3. the same durable run/thread resumes automatically without `/resume`, manual repository repair or a
   duplicate worktree/branch/PR;
4. one predeclared legitimate `risk_policy` interrupt is reached, for example an intentional change to
   a pre-existing public compatibility contract;
5. only that exceptional condition is presented to the operator through the normal decision channel;
6. the supervisor computes the checkpoint-bound candidate diff fingerprint both before and after the
   operator decision and rejects the run if the worktree changed while HITL was pending;
7. after approval, the run continues with no additional ordinary HITL until all mandatory requirements
   are PASS and LangGraph terminates as `converged`;
8. fresh read-only final audits re-read the resulting repository for requirements, architecture,
   compatibility and security, while evidence completeness is derived deterministically.

Routine model/provider/test/CI recovery must remain autonomous inside configured budgets. A test that
requires manual fixes for ordinary failures does not satisfy the release gate.

## Evidence emitted by the supervisor

The live supervisor writes three fixed artifacts in addition to the requested summary JSON:

```text
state_dir/acceptance/<run-id>/supervisor-progress.json
state_dir/evidence/<run-id>/external-final-audit.json
state_dir/evidence/<run-id>/external-acceptance-report.json
```

`supervisor-progress.json` records the process restart, automatic recovery, predeclared risk identity,
operator action, checkpoint-bound candidate SHA-256 and proof that no manual code edit occurred while
HITL was pending. `external-final-audit.json` records the exact final read-only audit lane, role,
verdict and requirements hash. These fixed artifacts are the provenance boundary for the release
report; the top-level supervisor JSON is not sufficient by itself.

After a live run, the report can be independently re-evaluated with:

```bash
converge-acceptance report \
  --run-id <durable-run-id> \
  --supervisor-evidence <acceptance-supervisor.json>
```

The report command cross-checks the supplied supervisor JSON against the fixed progress journal and
final-audit artifact for the same durable run. A structurally valid hand-authored PASS JSON therefore
cannot satisfy the canonical release gate by itself.

The verifier also reads the run's hash-pinned configuration, terminal LangGraph checkpoint, durable
resource budget and evidence bundle. It requires:

- an external GitHub target;
- terminal `converged` status;
- unchanged immutable requirements hash;
- digest-pinned container sandbox;
- a valid finite run-budget ledger;
- at least two distinct merged task cycles;
- complete task/diff/quality/risk/review/PR/CI evidence for each merged task;
- all required deterministic quality gates PASS;
- every configured independent review lane PASS;
- CI status PASS under an authoritative recorded GitHub policy containing at least one required status
  check; an unprotected branch with zero required checks cannot satisfy the release report;
- final mandatory compliance PASS;
- a real controller PID change followed by automatic same-run recovery;
- exactly the deliberately injected exceptional `risk_policy` HITL decision with no manual code edit;
- fresh final PASS checks for requirements, architecture, compatibility and security;
- matching supervisor-journal and final-audit provenance artifacts.

A missing, malformed, mismatched or ambiguous artifact is FAIL, not PASS. The deterministic report
verifier never calls a model and never mutates the target repository.

## Supervisor evidence contract

The summary JSON emitted by the live supervisor has this shape:

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

This summary is useful for transport and reporting, but release readiness additionally requires the
matching fixed artifacts generated during the live scenario. The provenance check is deliberately
procedural rather than a new PKI/signing subsystem; deployment filesystem integrity remains a separate
operational boundary.

## Readiness interpretation

Passing the internal Converge CI suite proves the orchestrator implementation. Passing this external
acceptance gate proves that the integrated product can turn frozen requirements into high-quality
repository changes across repeated autonomous cycles under real GitHub/model/runtime conditions.

Only after both are green should the project be described as generally ready for autonomous repository
development for the declared support scope. Go/Rust or other language claims still require their own
explicit deterministic compatibility scope; the acceptance result must not be generalized beyond what
Converge actually protects.
