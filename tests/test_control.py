from pathlib import Path

from converge_orchestrator.control import ControlSignals


def test_control_signals_are_durable_and_clearable(tmp_path: Path) -> None:
    first = ControlSignals(tmp_path)
    first.request_pause("run-1")
    second = ControlSignals(tmp_path)
    assert second.pause_requested("run-1")
    second.clear_pause("run-1")
    assert not first.pause_requested("run-1")
