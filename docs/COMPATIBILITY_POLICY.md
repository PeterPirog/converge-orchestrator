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
base revision; wildcard package targets and generated targets that were never tracked are not guessed.
Retargeting an existing public entry to a different present file remains review evidence rather than
automatic HITL.

## Node source exports and bounded local re-exports

For exact local source modules that remain published through a pre-existing `exports`, `main`,
`module`, `types` or `typings` entry, Converge parses JavaScript/TypeScript source with the Tree-sitter
TypeScript/TSX grammars. The target repository code is never imported or executed. Direct named
exports are compared structurally, and local wildcard barrels can be resolved through a deliberately
bounded module graph.

A baseline export that disappears from the same still-published consumer surface produces
`forbidden_public_api_change`. Additive named exports proceed autonomously. Explicit aliases and
explicit named re-exports are compared by their consumer-visible exported names, and declaration-file
exports are covered through the same parser.

Local `export * from "..."` resolution is intentionally narrower than full Node/TypeScript module
resolution:

- only relative specifiers are considered;
- every resolved path must remain inside the package root owning the published manifest;
- an exact supported source path is accepted directly;
- an extensionless path is accepted only when exactly one supported source or `index` candidate
  exists;
- traversal is bounded by maximum depth, unique-module count and re-export-edge count;
- cycles, external packages, escaped/encoded specifiers, package-root escapes, ambiguous targets,
  duplicate wildcard bindings and exhausted budgets make the surface incomplete rather than guessed.

The default resolver envelope is depth 4, 32 unique modules and 64 re-export edges. These are policy
safety bounds, not claims of full language semantics. A change in an internal module reached through
an otherwise unchanged public barrel is still considered when that module participated in the proven
base/candidate surface.

For callable declarations in TypeScript-family targets (`.ts`, `.tsx`, `.mts`, `.cts`, including
`.d.ts`), the parse records the **minimum number of positional call arguments** accepted by each
provable exported callable. `this` pseudo-parameters and rest parameters do not consume a required
call position; optional/defaulted parameters do not raise the minimum themselves, while a later
required parameter still makes preceding positions necessary. Multiple direct overload declarations
are represented by the least minimum they accept. Proven minimum-argument evidence is propagated
through a completely resolved local wildcard barrel. If the same still-published callable is provable
in both revisions and the candidate raises that minimum, the change produces
`forbidden_public_api_change` before semantic review.

This call-shape rule deliberately does not treat growth of a plain JavaScript parameter list as a
proven break, because JavaScript permits calls with fewer arguments. It also does not infer semantic
type assignability, callable-valued variables or arbitrary package/module-resolution behavior.

The adapter fails conservative rather than pretending to understand more than the parser and bounded
resolver prove:

- an incomplete baseline surface is not used as evidence that a public name existed;
- an incomplete candidate surface produces `observe` evidence when the changed paths intersect the
  inspected public surface, rather than a deterministic break/HITL;
- manifest retargeting is not compared as if the old and new modules were the same public source;
- `bin` and `browser` targets are not treated as named-module export surfaces;
- CommonJS assignment semantics, semantic type/signature equivalence, path aliases, package `imports`,
  dependency-package resolution and other full compiler/runtime resolution rules are not inferred by
  this policy.

This boundary is intentional: the deterministic gate blocks only a compatibility break it can prove.
Remaining Node work should add only high-confidence source-signature rules with parser-backed evidence;
uncertain semantics stay with tests and independent review rather than generating false HITL.

## Policy boundary

Compatibility findings are deterministic risk evidence, not an LLM verdict. The Builder cannot waive
them. Approval is valid only for the SHA-256 fingerprint of the reviewed candidate and expires after
any repair changes the diff. Additive compatibility work proceeds without human intervention.
