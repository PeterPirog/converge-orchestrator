from pathlib import Path

from converge_orchestrator.spec import compile_contract, is_read_only, sha256_file


def test_compile_contract_keeps_line_traceability_and_explicit_id(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Architecture\nARCH-017 Domain must not import infrastructure.\nOptional note.\n",
        encoding="utf-8",
    )
    contract = compile_contract(spec)
    assert contract.source.sha256 == sha256_file(spec)
    assert len(contract.requirements) == 1
    assert contract.requirements[0].id == "ARCH-017"
    assert "L2" in contract.requirements[0].source


def test_generated_requirement_id_is_stable(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text(
        "# Boundaries\nDomain must remain independent from adapters.\n",
        encoding="utf-8",
    )
    first = compile_contract(spec).requirements[0].id
    second = compile_contract(spec).requirements[0].id
    assert first == second
    assert first.startswith("REQ-")


def test_read_only_detection(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text("Service must be tested.\n", encoding="utf-8")
    spec.chmod(0o444)
    assert is_read_only(spec)
