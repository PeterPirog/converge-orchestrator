# Deterministic Python architecture boundaries

Converge can enforce explicit Python import boundaries before semantic review. This is an optional
quality policy, not a replacement for the immutable requirements Markdown. Configure a boundary only
when it is a deterministic projection of an architectural requirement you actually want to enforce.

```yaml
architecture:
  python_import_rules:
    - name: domain-does-not-depend-on-infrastructure
      source_paths:
        - src/acme/domain
      forbidden_imports:
        - acme.infrastructure
      allowed_imports:
        - acme.infrastructure.types
      forbid_relative_imports: false
      required: true
```

`source_paths` are literal repository-relative POSIX paths, not globs. A directory applies to all
Python files below it; a Python file path applies only to that file. `.` means the whole repository.
Paths containing parent traversal, absolute paths, platform-specific backslashes or glob syntax are
rejected during config validation.

`forbidden_imports` and `allowed_imports` are absolute Python module prefixes. An allowed prefix
overrides a broader forbidden prefix. Relative imports are unaffected unless
`forbid_relative_imports` is explicitly enabled.

## Monotonic policy

The analyzer parses configured Python sources with `ast` and compares the candidate against the
canonical baseline. Existing violations remain visible debt but do not block an unrelated incremental
change. A new required forbidden import, newly forbidden relative import, parse failure, or symlinked
source under the configured boundary fails the `architecture_imports` quality gate.

The baseline scan is cached outside the target repository. Cache validity is bound to the exact Git
base commit plus a SHA-256 of the architecture policy. A policy change or base commit change causes a
fresh baseline scan. If Git identity cannot be proven, Converge skips the cache and scans baseline
again instead of guessing.

Only bounded new-issue evidence is included in `quality.json`; counts are retained even when the list
is truncated. Candidate sources are always scanned fresh, so cache reuse cannot hide a newly
introduced architecture dependency.
