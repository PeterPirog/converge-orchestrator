# Revision 1.0 release record

## Status

Revision 1.0 is the first release-qualified baseline of Converge Orchestrator.

The canonical live external acceptance run completed successfully and the deterministic acceptance
report returned `ready=true`.

## Canonical provenance

- functional platform SHA:
  `7fa2d3f95a071dd3036492ac15896398aa607103`;
- canonical run ID:
  `5805a7a3e43640eda3147002de66c62d`;
- frozen Source of Truth SHA-256:
  `4e13ccb3dadb7fd273c16df4ca8c12421cd004aec02f4c7bfd1a4ae848ec98e6`;
- external target:
  `PeterPirog/converge-orchestrator-test-repo`;
- protected target branch:
  `converge-acceptance`;
- final target SHA:
  `c40dcc68f6e2189ad5f2f4f0a9c021f91f2defc6`;
- autonomous merged task cycles:
  12;
- controller restart:
  PID `33576 -> 18080`;
- automatic recovery observed:
  true;
- exceptional HITL:
  `risk_policy`;
- predeclared risk flag:
  `forbidden_public_api_change`;
- operator action:
  `approve`;
- manual code edit:
  none;
- deterministic evidence:
  PASS;
- final independent checks:
  requirements PASS, architecture PASS, compatibility PASS, security PASS, evidence PASS;
- canonical report:
  `ready=true`;
- external acceptance failure record:
  absent.

Issue #63 records the release-gate objective and was closed only after the canonical PASS evidence
existed.

## Release boundary

Revision 1.0 is qualified for the support scope actually enforced by its implementation, deterministic
gates, review lanes and external acceptance scenario. It does not claim universal autonomous correctness
for arbitrary languages, repositories or deployment topologies.

The V34 evidence belongs to the exact functional platform SHA above. Subsequent release-only metadata
or documentation commits do not retroactively change that provenance.

## Revision 2.0 hand-off

Revision 2.0 will be specified before it is implemented.

The intended process is:

1. author a materially improved Source of Truth for Revision 2.0;
2. resolve product policy, compatibility, migration, security, recovery and architecture decisions in
   that Source of Truth;
3. freeze the reviewed Source of Truth;
4. use Revision 1.0 Converge Orchestrator to develop this repository autonomously toward that target;
5. preserve Revision 1.0 as the reference baseline while Revision 2.0 evidence is generated.

Existing Revision 1.0 implementation details are current-state evidence. They MUST NOT become Revision
2.0 requirements merely because they already exist.
