from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from json import dumps
from pathlib import Path
from re import IGNORECASE
from re import compile as compile_regex
from stat import S_IWGRP, S_IWOTH, S_IWUSR
from typing import Literal

from .models import Contract, ContractSource, Requirement

_NORMATIVE = compile_regex(
    r"\b(must|shall|required|should|cannot|must not|nie może|musi|należy|powinien|wymag)\b",
    IGNORECASE,
)
_EXPLICIT_ID = compile_regex(r"\b([A-Z][A-Z0-9_-]{1,20}-\d{1,6})\b")
_RECOMMENDED = compile_regex(r"\b(should|powinien|powinna|powinno)\b", IGNORECASE)
_MANDATORY = compile_regex(
    r"\b(must|shall|required|cannot|must not|nie może|musi|należy|wymag)\b",
    IGNORECASE,
)


def classify_requirement_severity(statement: str) -> Literal["mandatory", "recommended"]:
    """Return the deterministic severity for a requirement statement (REQ-SOT-005).

    Pure and order-independent: any mandatory normative keyword classifies the
    statement as ``mandatory`` even when an advisory word is also present, and
    only advisory-only statements classify as ``recommended``. No I/O, no model
    calls, and no locale or environment dependence.
    """
    if _MANDATORY.search(statement):
        return "mandatory"
    if _RECOMMENDED.search(statement):
        return "recommended"
    return "mandatory"


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_read_only(path: Path) -> bool:
    mode = path.stat().st_mode
    write_bits = S_IWUSR | S_IWGRP | S_IWOTH
    return mode & write_bits == 0


def _stable_requirement_id(statement: str, heading: str) -> str:
    explicit = _EXPLICIT_ID.search(statement)
    if explicit:
        return explicit.group(1)
    material = f"{heading}\n{statement}".encode()
    return f"REQ-{sha256(material).hexdigest()[:10].upper()}"


def evaluate_explicit_identifier_uniqueness(records: Iterable[Requirement]) -> tuple[str, ...]:
    """Return deterministic clauses for duplicate explicit identifiers (REQ-SOT-003).

    Records whose identifier is a generated stable ID (``REQ-<10 hex>``) never match
    the explicit-identifier pattern and are exempt; every explicit identifier MUST be
    unique across the authoritative normative records, so each repeated explicit ID
    yields exactly one deterministic clause naming it.
    """
    explicit_counts: dict[str, int] = {}
    for record in records:
        explicit = _EXPLICIT_ID.search(record.id)
        if explicit is None:
            continue
        identifier = explicit.group(1)
        explicit_counts[identifier] = explicit_counts.get(identifier, 0) + 1
    return tuple(
        f"duplicate_explicit_identifier:{identifier}"
        for identifier, count in explicit_counts.items()
        if count > 1
    )


def evaluate_explicit_identifier_contract(records: Iterable[Requirement]) -> tuple[str, ...]:
    """Return deterministic clauses for generated-ID substitution of explicit IDs (REQ-SOT-007).

    A normative record whose single source line declares an explicit identifier MUST be
    assigned exactly that explicit identifier, never a generated one, so the
    Source-of-Truth explicit-ID single-line format compiles independently of generated
    identifiers. Records that declare no explicit identifier are exempt; each substituted
    declared identifier yields exactly one deterministic clause, in the fixed order the
    records were given, and a fully compliant set of records yields an empty tuple.
    """
    clauses: dict[str, None] = {}
    for record in records:
        declared = _EXPLICIT_ID.search(record.statement)
        if declared is None:
            continue
        identifier = declared.group(1)
        if record.id != identifier:
            clauses.setdefault(f"explicit_id_single_line_contract:{identifier}", None)
    return tuple(clauses)


def evaluate_severity_ambiguity(records: Iterable[Requirement]) -> tuple[str, ...]:
    """Return deterministic clauses for severity-ambiguous records (REQ-SOT-008).

    A normative record whose statement mixes mandatory and advisory language has no
    unambiguous severity, so the preflight MUST fail closed rather than guess; each
    ambiguous record yields exactly one deterministic clause naming its identifier, in
    the fixed order the records were given, and a fully unambiguous set of records
    yields an empty tuple.
    """
    clauses: dict[str, None] = {}
    for record in records:
        if _MANDATORY.search(record.statement) and _RECOMMENDED.search(record.statement):
            clauses.setdefault(f"severity_ambiguity:{record.id}", None)
    return tuple(clauses)


def evaluate_normative_record_recognition(records: Iterable[Requirement]) -> tuple[str, ...]:
    """Return deterministic clauses for unrecognised or fallback-extracted records (REQ-SOT-009).

    A production Source-of-Truth contract MUST contain recognized normative records, never
    records salvaged by the compiler fallback extraction (which by construction only runs
    when no record is recognized). When no record is recognized the preflight MUST fail
    closed naming ``contract_no_recognized_normative_records``, and when records were
    salvaged by the fallback it additionally names
    ``contract_relies_on_compiler_fallback_extraction`` in that fixed order; a contract
    whose records are all recognized yields an empty tuple.
    """
    records = tuple(records)
    if any(_NORMATIVE.search(record.statement) for record in records):
        return ()
    clauses = ["contract_no_recognized_normative_records"]
    if records:
        clauses.append("contract_relies_on_compiler_fallback_extraction")
    return tuple(clauses)


def evaluate_explicit_identifier_presence(records: Iterable[Requirement]) -> tuple[str, ...]:
    """Return deterministic clauses for compiled requirements lacking explicit IDs (REQ-SOT-009).

    Every compiled requirement MUST carry the unique explicit stable identifier declared by
    its authoritative source line; a generated stable ID (``REQ-<10 hex>``) is proof that the
    source line declared none, so each such record yields exactly one deterministic clause
    naming its identifier, in the fixed order the records were given, and a fully explicit
    set of records yields an empty tuple.
    """
    clauses: dict[str, None] = {}
    for record in records:
        if _EXPLICIT_ID.search(record.id) is None:
            clauses.setdefault(f"requirement_without_explicit_stable_identifier:{record.id}", None)
    return tuple(clauses)


def compile_contract(path: Path) -> Contract:
    """Compile Markdown into traceable records without replacing the source text."""
    requirements: list[Requirement] = []
    heading = "root"
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if text.startswith("#"):
            heading = text.lstrip("#").strip() or heading
            continue
        candidate = text.lstrip("-*0123456789. ").strip()
        if not candidate or len(candidate) < 12 or not _NORMATIVE.search(candidate):
            continue
        severity = classify_requirement_severity(candidate)
        requirements.append(
            Requirement(
                id=_stable_requirement_id(candidate, heading),
                statement=candidate,
                source=f"{path.name}:L{line_no} [{heading}]",
                severity=severity,
            )
        )
    if not requirements:
        for line_no, raw in enumerate(lines, start=1):
            candidate = raw.strip().lstrip("-*0123456789. ").strip()
            if len(candidate) < 24 or candidate.startswith("#"):
                continue
            requirements.append(
                Requirement(
                    id=_stable_requirement_id(candidate, heading),
                    statement=candidate,
                    source=f"{path.name}:L{line_no}",
                )
            )
    clauses = evaluate_explicit_identifier_uniqueness(requirements)
    if clauses:
        raise ValueError(
            "contract preflight rejected duplicate explicit identifiers: " + "; ".join(clauses)
        )
    clauses = evaluate_explicit_identifier_contract(requirements)
    if clauses:
        raise ValueError(
            "contract preflight rejected generated-ID substitution of explicit "
            "identifiers: " + "; ".join(clauses)
        )
    return Contract(
        source=ContractSource(path=str(path.resolve()), sha256=sha256_file(path)),
        requirements=requirements,
    )


def preflight_contract(path: Path, expected_sha256: str | None = None) -> Contract:
    """Expose the compiled contract through a fail-closed Source-of-Truth preflight.

    Deterministic and offline: no network, model, git, or subprocess access. Returns the
    compiled contract for a read-only Source of Truth whose records are all recognized
    normative records carrying unique explicit stable identifiers; otherwise fails closed
    with ``ValueError`` naming the clauses in a fixed order: explicit-ID substitution and
    duplicate explicit identifiers (raised by ``compile_contract``), then source digest
    integrity (``contract_source_digest_mismatch``) and read-only integrity
    (``source_not_read_only``), then normative identity (REQ-SOT-009): no recognized
    normative records (``contract_no_recognized_normative_records``), reliance on compiler
    fallback extraction (``contract_relies_on_compiler_fallback_extraction``), and each
    requirement without an explicit stable identifier
    (``requirement_without_explicit_stable_identifier:<id>``), then severity ambiguity
    (``severity_ambiguity:<id>``).
    """
    integrity_clauses: list[str] = []
    if expected_sha256 is not None and sha256_file(path) != expected_sha256:
        integrity_clauses.append("contract_source_digest_mismatch")
    if not is_read_only(path):
        integrity_clauses.append("source_not_read_only")
    contract = compile_contract(path)
    identity_clauses = evaluate_normative_record_recognition(
        contract.requirements
    ) + evaluate_explicit_identifier_presence(contract.requirements)
    clauses = (
        tuple(integrity_clauses)
        + identity_clauses
        + evaluate_severity_ambiguity(contract.requirements)
    )
    if clauses:
        raise ValueError("contract preflight rejected Source of Truth: " + "; ".join(clauses))
    return contract


def evaluate_contract_evidence_preservation(contract: Contract) -> tuple[str, ...]:
    """Return deterministic clauses for provenance missing from contract evidence (REQ-SOT-004).

    Durable contract evidence MUST preserve each requirement's identifier,
    statement, severity, and source location plus a non-empty Source-of-Truth
    digest; every blank value yields exactly one deterministic clause so
    ``write_contract`` can fail closed before persisting incomplete provenance.
    """
    clauses: list[str] = []
    if not str(contract.source.sha256).strip():
        clauses.append("contract.source.sha256_blank")
    for index, record in enumerate(contract.requirements):
        for field in ("id", "statement", "severity", "source"):
            if not str(getattr(record, field)).strip():
                clauses.append(f"requirements[{index}].{field}_blank")
    return tuple(clauses)


def write_contract(path: Path, contract: Contract) -> None:
    clauses = evaluate_contract_evidence_preservation(contract)
    if clauses:
        raise ValueError(
            "contract evidence rejected incomplete provenance: " + "; ".join(clauses)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = contract.model_dump(mode="json")
    path.write_text(
        dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
