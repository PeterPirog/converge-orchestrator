# Changelog

This project records user-visible release baselines here. Detailed historical implementation milestones
remain in [docs/ROADMAP.md](docs/ROADMAP.md).

## 1.0.0 — 2026-10-10

Revision 1.0 is the first release-qualified Converge Orchestrator baseline.

Release evidence:

- qualified functional platform: `7fa2d3f95a071dd3036492ac15896398aa607103`;
- canonical external acceptance run: `5805a7a3e43640eda3147002de66c62d`;
- frozen requirements SHA-256:
  `4e13ccb3dadb7fd273c16df4ca8c12421cd004aec02f4c7bfd1a4ae848ec98e6`;
- external target final SHA: `c40dcc68f6e2189ad5f2f4f0a9c021f91f2defc6`;
- 12 autonomous merged task/PR/CI cycles;
- real controller restart with automatic same-run recovery;
- exactly one predeclared exceptional `risk_policy` approval;
- no manual code edit;
- terminal convergence;
- final requirements, architecture, compatibility, security and evidence audits PASS;
- canonical release report: `ready=true`;
- no failed acceptance checks and no external-acceptance failure record.

See [docs/REVISION_1_0.md](docs/REVISION_1_0.md) for the release provenance boundary and the
Revision 2.0 development hand-off.
