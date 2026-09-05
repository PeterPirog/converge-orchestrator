# Converge external acceptance requirements

This document is the reviewed acceptance fixture for the isolated
`PeterPirog/converge-orchestrator-test-repo:converge-acceptance` branch. It is not a roadmap for the
repository's normal `main` line.

During the live release scenario this file is copied outside both the target repository and its writable
worktrees, reviewed, made physically read-only, and used as Converge's immutable Source of Truth for that
run.

## ACCEPT-001 — Structured command simulation

**Severity:** mandatory

`shared_tools.fake_terminal` must provide an additive public function
`simulate_command(command: str)` that returns an immutable structured result containing at least the
original command text, simulated stdout text, integer exit code `0`, and an explicit `simulated=True`
marker.

The implementation must remain a simulator: it must not call `subprocess`, `os.system`, a shell, or any
other operating-system command execution API. Existing `run_command(command: str) -> str` behavior must
remain compatible while this requirement is implemented.

Deterministic tests must prove the returned structure and prove that the command text is treated as data
rather than executed.

## ACCEPT-002 — Deterministic secret redaction helper

**Severity:** mandatory

Add `shared_tools.redaction.redact_secrets(text: str, secret_values: Iterable[str]) -> str` as a small,
pure helper for training logs. Every non-empty supplied secret value that occurs in `text` must be
replaced with the exact literal `[REDACTED]`. Empty secret values must be ignored. Repeated occurrences
and overlapping inputs must produce deterministic output independent of set/hash iteration order.

The helper must not read environment variables, files, network resources, or process state. It must not
log either the input text or secret values. Deterministic tests must cover repeated values, empty values,
and overlapping secret values.

## ACCEPT-003 — Deliberate compatibility exception for release-gate HITL proof

**Severity:** mandatory

For this acceptance branch only, replace the public helper
`shared_tools.fake_terminal.format_output(output: str) -> str` with
`format_terminal_output(output: str) -> str`, preserving the exact fenced-terminal rendering behavior.
The old public symbol `format_output` must no longer be exported by that module after the migration, and
the target tests/documentation on this branch must use the new name.

This is an intentionally breaking public-API migration whose sole purpose is to exercise Converge's
exception-based HITL release gate. It is the predeclared `forbidden_public_api_change` condition for the
live acceptance supervisor. Operator approval may authorize this requirement-specific exception, but it
must not waive deterministic tests, independent correctness/architecture/security review, GitHub CI, or
any other gate.

## Convergence constraints

- Keep changes confined to the smallest relevant `shared_tools`, tests, and acceptance-branch
  documentation surfaces.
- Do not modify GitHub Actions, branch protection, Converge configuration, or this requirements file as
  part of an implementation task.
- Do not add network calls, command execution, secrets, or new runtime dependencies to satisfy these
  requirements.
- The final acceptance state requires all three mandatory requirements to be satisfied with no unrelated
  refactor or feature work.
