import json
from pathlib import Path

from converge_orchestrator.evidence import EvidenceStore


def test_evidence_store_writes_task_artifacts_and_events(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    artifact = store.write_json("run-1", "task-1", "quality.json", {"ok": True})
    store.append_event("run-1", "quality", {"task_id": "task-1"})
    assert json.loads(artifact.read_text()) == {"ok": True}
    events = (tmp_path / "run-1" / "events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["event"] == "quality"
