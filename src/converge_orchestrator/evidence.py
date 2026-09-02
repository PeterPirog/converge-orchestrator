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

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
