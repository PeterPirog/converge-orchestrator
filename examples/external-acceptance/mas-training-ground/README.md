# `mas-training-ground` external acceptance fixture

This fixture is the concrete release-gate target described by
[`docs/EXTERNAL_ACCEPTANCE.md`](../../../docs/EXTERNAL_ACCEPTANCE.md). It exists to prove Converge on a
repository outside the orchestrator itself without changing that repository's normal `main` line.

## Prepared remote target

Repository: `PeterPirog/mas-training-ground`

Base branch: `converge-acceptance`

The branch is isolated from `main` and contains:

- deterministic baseline tests for `shared_tools.fake_terminal`;
- an `Acceptance CI` pull-request workflow scoped to `converge-acceptance`;
- a branch marker explaining that changes on this line are Converge release-test evidence only.

The observed required-check candidate is the GitHub Actions job context `test` from the `Acceptance CI`
workflow.

## One external policy step before the live run

GitHub branch protection / a Ruleset must be enabled for `converge-acceptance` and must require the
`test` status check. This repository-side policy cannot be substituted by a green workflow that merely
happens to run: Converge's acceptance preflight requires an authoritative required-check policy.

After the policy is configured, verify it before any model work:

```bash
converge-acceptance preflight --config /absolute/path/to/converge.yaml
```

The command must report an authoritative policy and at least the required `test` check.

## Requirements source

Use [`requirements.md`](requirements.md) as the reviewed acceptance specification, but do not point
Converge directly at this writable checkout. Copy the file to the deployment's project-control
directory outside the target repository and make that copy physically read-only. That copy becomes the
only immutable Source of Truth for the live run.

The fixture has three mandatory requirements. Two exercise ordinary autonomous implementation; the
third deliberately requests one public-API compatibility exception so the supervisor can prove that
HITL occurs only for the predeclared `forbidden_public_api_change` condition.

## Local deployment prerequisites

Use the normal `examples/converge.yaml` as the configuration baseline and change only project-specific
values. For this fixture the important values are:

```yaml
project:
  name: mas-training-ground-acceptance
  repo_path: /absolute/path/to/mas-training-ground
  requirements_path: /absolute/path/to/read-only/requirements.md
  require_spec_read_only: true

github:
  repo: PeterPirog/mas-training-ground
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

The production acceptance configuration must additionally retain all three independent review lanes,
finite run/model budgets and `sandbox.mode: container` with a locally available digest-pinned image.
The image must contain stable OpenCode and Python/pytest. Provision the internal agent network and model
credentials through the deployment environment; never put secrets in this fixture or target repo.

Run normal deployment validation before the supervisor:

```bash
converge doctor --config /absolute/path/to/converge.yaml
converge-acceptance preflight --config /absolute/path/to/converge.yaml
```

Then execute the canonical live scenario:

```bash
converge-acceptance supervise \
  --config /absolute/path/to/converge.yaml \
  --project-id mas-training-ground-acceptance \
  --expected-risk-flag forbidden_public_api_change \
  --output /absolute/path/to/acceptance-supervisor.json
```

A release PASS requires the same durable run to reach convergence through at least two merged task/PR/CI
cycles, survive the supervisor's controller restart, require no manual code edits, exercise exactly the
planned exceptional risk decision, and pass the final independent requirements/architecture/
compatibility/security/evidence audit. Do not generalize this Python acceptance result to language
semantics Converge does not claim to protect.
