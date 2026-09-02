from converge_orchestrator.models import GateResult
from converge_orchestrator.quality import required_gates_pass


def test_optional_gate_does_not_block() -> None:
    results = [GateResult(name="tests", ok=True, required=True, returncode=0, output=""), GateResult(name="coverage", ok=False, required=False, returncode=1, output="")]
    assert required_gates_pass(results)


def test_required_gate_blocks() -> None:
    results = [GateResult(name="tests", ok=False, required=True, returncode=1, output="boom")]
    assert not required_gates_pass(results)
