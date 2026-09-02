from pathlib import Path

from converge_orchestrator.registry import ControlRegistry


def test_registry_persists_projects_and_runs(tmp_path: Path) -> None:
    registry = ControlRegistry(tmp_path / "control.sqlite")
    config_path = tmp_path / "project.yaml"
    project = registry.register_project("payments", config_path)
    assert project["id"] == "payments"
    assert project["config_path"] == str(config_path.resolve())

    run = registry.create_run("run-1", "payments", "thread-1")
    assert run["status"] == "queued"
    registry.update_run(
        "run-1",
        status="interrupted",
        node="human",
        active_task_id="ARCH-017-1",
    )
    restored = registry.get_run("run-1")
    assert restored["node"] == "human"
    assert restored["active_task_id"] == "ARCH-017-1"
