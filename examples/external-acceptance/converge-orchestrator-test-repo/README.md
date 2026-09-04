# `converge-orchestrator-test-repo` external acceptance fixture

This is the preferred purpose-built target for the live release gate described in
[`docs/EXTERNAL_ACCEPTANCE.md`](../../../docs/EXTERNAL_ACCEPTANCE.md).

## Prepared remote target

Repository: `PeterPirog/converge-orchestrator-test-repo`

Base branch: `converge-acceptance`

The target branch currently contains a minimal Python package, deterministic baseline tests and an
`Acceptance CI` workflow. A bootstrap PR into `converge-acceptance` passed the observed GitHub Actions
job context `test`, proving the remote CI workflow itself is functional.

The branch is intentionally isolated from `main` so acceptance runs can create, merge and reset changes
without affecting any real project.

## Required remote policy before model work

The acceptance branch must have branch protection or an effective Ruleset that requires the `test`
status check. A workflow that merely runs is insufficient because Converge treats GitHub required CI as
the authoritative integration gate.

At fixture creation time GitHub reports `converge-acceptance` as unprotected and no repository Rulesets
are present. Therefore the canonical acceptance supervisor must not start yet; `converge-acceptance
preflight` is expected to fail closed before any model budget is consumed.

Once the remote policy is configured, verify:

```bash
converge-acceptance preflight --config /absolute/path/to/converge.yaml
```

The result must be authoritative and include required check context `test`.

## Requirements source

Use [`requirements.md`](requirements.md) as the reviewed acceptance specification, but copy it outside
the writable target checkout before running Converge. The deployment copy must be physically read-only
and becomes the only immutable Source of Truth for the live run.

The three mandatory requirements are deliberately shaped to exercise the product rather than a demo:

1. one additive behavior change with deterministic security constraints;
2. one independent pure helper with overlapping-input edge cases;
3. one explicitly predeclared public-API migration that must trigger exactly one
   `forbidden_public_api_change` HITL decision.

## Project-specific configuration

Start from `examples/converge.yaml` and change only project/deployment values. At minimum:

```yaml
project:
  name: converge-orchestrator-external-acceptance
  repo_path: /absolute/path/to/converge-orchestrator-test-repo
  requirements_path: /absolute/path/to/read-only/requirements.md
  require_spec_read_only: true

github:
  repo: PeterPirog/converge-orchestrator-test-repo
  base_branch: converge-acceptance
  auto_merge: true
  merge_method: squash

quality:
  auto_discover: false
  gates:
    - name: acceptance-tests
      command: [python, -m, pytest, -q, tests/test_shared_tools_fake_terminal.py]
      required: true
      timeout_seconds: 600
```

Retain all three independent review lanes, finite per-run model/time budgets and `sandbox.mode:
container` with a locally available digest-pinned image containing stable OpenCode plus Python/pytest.
Model and GitHub credentials belong in the deployment environment, never in the target repository,
requirements, prompts or evidence.

## Canonical execution

```bash
converge doctor --config /absolute/path/to/converge.yaml
converge-acceptance preflight --config /absolute/path/to/converge.yaml
converge-acceptance supervise \
  --config /absolute/path/to/converge.yaml \
  --project-id converge-orchestrator-external-acceptance \
  --expected-risk-flag forbidden_public_api_change \
  --output /absolute/path/to/acceptance-supervisor.json
```

Release PASS requires the same durable run to produce at least two merged task/PR/required-CI cycles,
survive the supervisor-controlled controller restart, require no manual code edits, exercise only the
predeclared exceptional risk decision, converge all mandatory requirements and pass the independent
requirements/architecture/compatibility/security/evidence audit.
