from __future__ import annotations

from pathlib import Path


class ControlSignals:
    """Filesystem-backed cooperative control flags checked only at safe graph boundaries."""

    def __init__(self, state_dir: Path):
        self.root = state_dir / "control"
        self.root.mkdir(parents=True, exist_ok=True)

    def _pause_path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.pause"

    def request_pause(self, run_id: str) -> None:
        path = self._pause_path(run_id)
        temporary = path.with_suffix(".pause.tmp")
        temporary.write_text("pause\n", encoding="utf-8")
        temporary.replace(path)

    def pause_requested(self, run_id: str) -> bool:
        return self._pause_path(run_id).exists()

    def clear_pause(self, run_id: str) -> None:
        self._pause_path(run_id).unlink(missing_ok=True)
