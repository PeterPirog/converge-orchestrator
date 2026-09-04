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

Converge also protects the **existence of exact local files that remain published by a pre-existing
package contract**. When a changed file is deleted, the risk classifier checks `package.json` files in
its package/ancestor scopes. If an existing public `exports`, `bin`, `main`, `module`, `types`,
`typings` or `browser` entry still points to that exact local file in the candidate manifest, deletion
is a definite compatibility break and produces `forbidden_public_api_change` even when the manifest
itself was not edited. This check is deliberately limited to exact `./...` targets that existed in the
base revision; wildcard exports and generated targets that were never tracked are not guessed.
Retargeting an existing public entry to a different present file remains review evidence rather than
automatic HITL.

## Node direct source exports

For exact local source modules that remain published through a pre-existing `exports`, `main`,
`module`, `types` or `typings` entry, Converge additionally parses changed JavaScript/TypeScript source
with the Tree-sitter TypeScript/TSX grammars. The target repository code is never imported or executed.
The adapter compares only the direct top-level syntactic export names that it can prove from both the
canonical base and candidate module.

A direct baseline export that disappears from the same still-published module produces
`forbidden_public_api_change`. Additive named exports proceed autonomously. Explicit aliases and
explicit named re-exports are compared by their consumer-visible exported names, and declaration-file
exports are covered through the same parser.

For direct callable declarations in TypeScript-family targets (`.ts`, `.tsx`, `.mts`, `.cts`, including
`.d.ts`), the same parse also records the **minimum number of positional call arguments** accepted by
each exported callable. `this` pseudo-parameters and rest parameters do not consume a required call
position; optional/defaulted parameters do not raise the minimum themselves, while a later required
parameter still makes preceding positions necessary. Multiple direct overload declarations are
represented by the least minimum they accept. If the same still-published direct callable is provable
in both revisions and the candidate raises that minimum, the change produces
`forbidden_public_api_change` before semantic review.

This call-shape rule deliberately does not treat growth of a plain JavaScript parameter list as a
proven break, because JavaScript permits calls with fewer arguments. It also does not resolve callable
variables, aliases, explicit re-exports, wildcard re-export graphs or semantic type assignability.
Those cases remain available to tests and independent semantic review instead of generating guessed
HITL.

The adapter deliberately fails conservative rather than pretending to understand more than the syntax
proves:

- an incomplete baseline surface is not used as evidence that a public name existed;
- a candidate containing unresolved wildcard exports, unsupported/ambiguous export syntax or parser
  errors produces `observe` evidence for independent review instead of a deterministic break/HITL;
- manifest retargeting is not compared as if the old and new modules were the same public source;
- `bin` and `browser` targets are not treated as named-module export surfaces;
- CommonJS assignment semantics, wildcard re-export graphs, module resolution and broader source-level
  type/signature equivalence are not inferred by this policy.

This boundary is intentional: the deterministic gate blocks only a compatibility break it can prove.
The next Node compatibility work is bounded local re-export resolution and additional conservative
source-signature rules, not regex-based approximation.

## Policy boundary

Compatibility findings are deterministic risk evidence, not an LLM verdict. The Builder cannot waive
them. Approval is valid only for the SHA-256 fingerprint of the reviewed candidate and expires after
any repair changes the diff. Additive compatibility work proceeds without human intervention.
