# Deterministic compatibility policy

Converge protects selected consumer-visible interfaces before semantic review. The policy is
conservative: it uses parsers for contracts it can identify deterministically and does not guess at
JavaScript or TypeScript semantics with regular expressions.

## Python public API

For changed public Python modules, Converge parses the canonical base and candidate with the standard
library AST. It detects removal or signature changes of public module functions, classes and methods.
An explicit literal `__all__` limits the exported surface. Private modules and private symbols are not
treated as public API.

## Node package contract

For every changed `package.json`, Converge compares these existing consumer-visible entries against
the canonical base branch:

- package name;
- `exports`, including subpath and conditional exports;
- `bin` commands;
- legacy `main`, `module`, `types`, `typings` and `browser` entry points.

Adding a new export or command is compatible and does not interrupt the run. Removing an existing
entry or changing the package name produces `forbidden_public_api_change`, bound to the exact
candidate diff. Retargeting an existing entry is retained as `observe` evidence for independent
review, but does not cause HITL by itself because a build-output path change can preserve the public
API. The preferred autonomous repair for an actual break is additive: retain the old entry as a
compatibility shim and add the new entry separately. A genuinely intentional breaking change still
requires explicit risk approval and cannot bypass tests, independent review or CI.

The adapter deliberately does not infer named source-code exports. Correctly parsing the full
JavaScript/TypeScript module grammar and resolver rules requires a stack-native analyzer; a partial
regex parser would create false confidence. Source-level Node, Go and Rust compatibility remain
future adapters.

## Policy boundary

Compatibility findings are deterministic risk evidence, not an LLM verdict. The Builder cannot waive
them. Approval is valid only for the SHA-256 fingerprint of the reviewed candidate and expires after
any repair changes the diff. Additive compatibility work proceeds without human intervention.
