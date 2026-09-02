from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, run_id: str, task_id: str) -> Path:
        target = self.root / run_id / task_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, run_id: str, task_id: str, name: str, payload: Any) -> Path:
        path = self.task_dir(run_id, task_id) / name
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path

    def write_text(self, run_id: str, task_id: str, name: str, text: str) -> Path:
        path = self.task_dir(run_id, task_id) / name
        self._atomic_write(path, text)
        return path

    def append_event(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": payload,
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_task_bundle(self, run_id: str, task_id: str) -> dict[str, Any]:
        target = self.root / run_id / task_id
        if not target.is_dir():
            raise FileNotFoundError(target)
        artifacts: dict[str, Any] = {}
        for path in sorted(target.iterdir()):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix == ".json":
                try:
                    artifacts[path.name] = json.loads(text)
                    continue
                except json.JSONDecodeError:
                    pass
            artifacts[path.name] = text
        return {"run_id": run_id, "task_id": task_id, "artifacts": artifacts}

    def find_task_bundles(self, task_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        if not self.root.exists():
            return matches
        for run_dir in sorted(self.root.iterdir()):
            if not run_dir.is_dir():
                continue
            target = run_dir / task_id
            if target.is_dir():
                matches.append(self.read_task_bundle(run_dir.name, task_id))
        return matches

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
