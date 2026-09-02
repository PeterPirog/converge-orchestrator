from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .models import QualityGate, StackProfile


def _declared_python_dependency(data: dict[str, Any], package: str) -> bool:
    project = data.get("project", {}) if isinstance(data, dict) else {}
    dependencies = list(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {}) or {}
    if isinstance(optional, dict):
        for values in optional.values():
            dependencies.extend(values or [])
    prefix = package.lower()
    return any(str(item).lower().startswith(prefix) for item in dependencies)


def _python_gates(root: Path) -> tuple[list[str], list[QualityGate]]:
    indicators = [name for name in ("pyproject.toml", "requirements.txt") if (root / name).is_file()]
    if not indicators:
        return [], []

    data: dict[str, Any] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError:
            data = {}
    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    gates: list[QualityGate] = []

    pytest_declared = (
        isinstance(tool, dict)
        and "pytest" in tool
        or _declared_python_dependency(data, "pytest")
        or (root / "pytest.ini").is_file()
    )
    if pytest_declared:
        gates.append(
            QualityGate(
                name="auto:python:test",
                command=["python", "-m", "pytest", "-q"],
            )
        )

    ruff_declared = (
        isinstance(tool, dict)
        and "ruff" in tool
        or _declared_python_dependency(data, "ruff")
        or (root / "ruff.toml").is_file()
        or (root / ".ruff.toml").is_file()
    )
    if ruff_declared:
        gates.append(QualityGate(name="auto:python:lint", command=["ruff", "check", "."]))

    mypy_declared = (
        isinstance(tool, dict)
        and "mypy" in tool
        or _declared_python_dependency(data, "mypy")
        or (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
    )
    if mypy_declared:
        gates.append(QualityGate(name="auto:python:typecheck", command=["mypy", "."]))
    return indicators, gates


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _node_gates(root: Path) -> tuple[list[str], list[QualityGate], str | None]:
    package = root / "package.json"
    if not package.is_file():
        return [], [], None
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["package.json"], [], _node_package_manager(root)
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    if not isinstance(scripts, dict):
        scripts = {}
    manager = _node_package_manager(root)
    gates: list[QualityGate] = []
    for script, logical_name in (
        ("test", "test"),
        ("lint", "lint"),
        ("typecheck", "typecheck"),
        ("build", "build"),
    ):
        if script not in scripts:
            continue
        if manager == "npm":
            command = ["npm", "test"] if script == "test" else ["npm", "run", script]
        else:
            command = [manager, script]
        gates.append(QualityGate(name=f"auto:node:{logical_name}", command=command))
    return ["package.json"], gates, manager


def inspect_repository(root: Path) -> StackProfile:
    root = root.resolve()
    stacks: list[str] = []
    indicators: dict[str, list[str]] = {}
    gates: list[QualityGate] = []
    package_manager: str | None = None

    python_indicators, python_gates = _python_gates(root)
    if python_indicators:
        stacks.append("python")
        indicators["python"] = python_indicators
        gates.extend(python_gates)

    node_indicators, node_gates, package_manager = _node_gates(root)
    if node_indicators:
        stacks.append("node")
        indicators["node"] = node_indicators
        gates.extend(node_gates)

    if (root / "go.mod").is_file():
        stacks.append("go")
        indicators["go"] = ["go.mod"]
        gates.append(QualityGate(name="auto:go:test", command=["go", "test", "./..."]))

    if (root / "Cargo.toml").is_file():
        stacks.append("rust")
        indicators["rust"] = ["Cargo.toml"]
        gates.append(
            QualityGate(name="auto:rust:test", command=["cargo", "test", "--all"])
        )

    return StackProfile(
        stacks=stacks,
        indicators=indicators,
        quality_gates=gates,
        package_manager=package_manager,
    )
