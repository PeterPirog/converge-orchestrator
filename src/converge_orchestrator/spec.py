from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .models import Requirement


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_contract(path: Path) -> list[Requirement]:
    """Compile Markdown into stable, traceable requirement records without semantic rewriting."""
    requirements: list[Requirement] = []
    heading = "root"
    counter = 1
    normative = re.compile(
        r"\b(must|shall|required|should|cannot|must not|nie może|musi|należy|powinien|wymag)\b",
        re.IGNORECASE,
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if text.startswith("#"):
            heading = text.lstrip("#").strip() or heading
            continue
        candidate = text.lstrip("-*0123456789. ").strip()
        if not candidate or len(candidate) < 12:
            continue
        if normative.search(candidate):
            requirements.append(Requirement(id=f"REQ-{counter:04d}", statement=candidate, source=f"{path.name}:L{line_no} [{heading}]"))
            counter += 1
    if not requirements:
        for line_no, raw in enumerate(lines, start=1):
            candidate = raw.strip().lstrip("-*0123456789. ").strip()
            if len(candidate) >= 24 and not candidate.startswith("#"):
                requirements.append(Requirement(id=f"REQ-{counter:04d}", statement=candidate, source=f"{path.name}:L{line_no}"))
                counter += 1
    return requirements


def write_contract(path: Path, requirements: list[Requirement]) -> None:
    path.write_text(json.dumps([r.model_dump(mode="json") for r in requirements], ensure_ascii=False, indent=2), encoding="utf-8")
