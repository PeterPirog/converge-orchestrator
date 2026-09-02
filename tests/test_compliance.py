from converge_orchestrator.compliance import ComplianceEngine
from converge_orchestrator.models import Contract, ContractSource, Requirement, RequirementStatus


def test_compliance_moves_from_unverified_to_partial_to_pass() -> None:
    contract = Contract(
        source=ContractSource(path="architecture.md", sha256="abc"),
        requirements=[Requirement(id="ARCH-1", statement="Rule", source="architecture.md:L1")],
    )
    snapshot = ComplianceEngine.initial(contract)
    assert snapshot.entries["ARCH-1"].status == RequirementStatus.UNVERIFIED
    snapshot = ComplianceEngine.mark_local_pass(snapshot, ["ARCH-1"], ["local"])
    assert snapshot.entries["ARCH-1"].status == RequirementStatus.PARTIAL
    snapshot = ComplianceEngine.mark_remote_verified(snapshot, ["ARCH-1"], ["ci"])
    assert snapshot.entries["ARCH-1"].status == RequirementStatus.PASS
