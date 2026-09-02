---
name: requirements-compliance
description: Trace code changes to immutable architecture requirements and detect drift.
---
# Requirements compliance
1. Treat the supplied architecture Markdown as authoritative and read-only.
2. Cite requirement IDs/source lines in plans and reviews.
3. Never resolve ambiguity by changing requirements.
4. Prefer measurable evidence: tests, dependency checks, static analysis, build output.
5. A change must not turn an already-satisfied mandatory requirement into a violation.
