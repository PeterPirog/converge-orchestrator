from __future__ import annotations

from hashlib import sha256
from json import dumps
from pathlib import Path
from re import IGNORECASE, compile as compile_regex
from stat import S_IWGRP, S_IWOTH, S_IWUSR

from .models import Contract, ContractSource, Requirement


_NORMATIVE = compile_regex(
    r"\b(must|shall|required|should|cannot|must not|nie może|musi|należy|powinien|wymag)\b",
    IGNORECASE,
)
_EXPLICIT_ID = compile_regex(r"\b([A-Z][A-Z0-9_-]{1,20}-\d{1,6})\b")
_RECOMMENDED = compile_regex(r"\b(should|powinien|powinna|powinno)\b", IGNORECASE)


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
        severity = "recommended" if _RECOMMENDED.search(candidate) else "mandatory"
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
    return Contract(
        source=ContractSource(path=str(path.resolve()), sha256=sha256_file(path)),
        requirements=requirements,
    )


def write_contract(path: Path, contract: Contract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = contract.model_dump(mode="json")
    path.write_text(
        dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
