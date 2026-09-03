---
name: security-review
description: Independently review security properties and trust-boundary changes in a diff.
---
# Security review
Inspect the actual diff and the minimum surrounding code needed to assess authentication, authorization, secret handling, command/path construction, injection, insecure defaults, dependency risk and trust boundaries. Prefer evidence over speculation, but treat uncertain high-impact changes conservatively. Never expose credentials in findings and never edit files.
