from pathlib import Path

from converge_orchestrator.spec import compile_contract, sha256_file


def test_compile_contract_keeps_line_traceability(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text("# Architecture\nDomain must not import infrastructure.\nOptional note.\n", encoding="utf-8")
    result = compile_contract(spec)
    assert len(result) == 1
    assert result[0].id == "REQ-0001"
    assert "L2" in result[0].source


def test_hash_changes_when_spec_changes(tmp_path: Path) -> None:
    spec = tmp_path / "architecture.md"
    spec.write_text("A", encoding="utf-8")
    before = sha256_file(spec)
    spec.write_text("B", encoding="utf-8")
    assert sha256_file(spec) != before
