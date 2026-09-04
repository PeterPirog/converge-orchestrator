from __future__ import annotations

from typing import Any, Protocol

from .workspace_identity import WorkspaceAffinityError


class AffinityController(Protocol):
    registry: Any

    def _config_for_project(self, project: dict[str, Any]) -> Any: ...

    def _config_for_run(self, record: dict[str, Any]) -> Any: ...


def project_affinity(controller: AffinityController, project_id: str) -> dict[str, Any]:
    """Classify whether this worker can safely execute one registered project.

    The probe is deliberately read-only. When a durable run is unfinished its pinned run
    configuration is authoritative for placement; mutable source YAML is consulted only when no
    unfinished run exists and a future run would be started from current project configuration.
    """
    project = controller.registry.get_project(project_id)
    history = controller.registry.runs_for_project(project_id)
    unfinished = [record for record in history if not record.get("finished_at")]

    if len(unfinished) > 1:
        return {
            "project_id": project_id,
            "eligible": False,
            "basis": "ambiguous",
            "reason": "multiple_unfinished_runs",
            "unfinished_runs": len(unfinished),
        }

    active = unfinished[0] if unfinished else None
    basis = "pinned_run" if active is not None else "project_config"
    try:
        if active is not None:
            controller._config_for_run(active)
        else:
            controller._config_for_project(project)
    except WorkspaceAffinityError:
        reason = "affinity_mismatch"
    except (FileNotFoundError, OSError):
        reason = "storage_unavailable"
    except ValueError:
        reason = "configuration_invalid"
    except RuntimeError:
        reason = "run_configuration_invalid" if active is not None else "configuration_invalid"
    else:
        return {
            "project_id": project_id,
            "eligible": True,
            "basis": basis,
            "reason": "local",
            "unfinished_runs": len(unfinished),
        }

    return {
        "project_id": project_id,
        "eligible": False,
        "basis": basis,
        "reason": reason,
        "unfinished_runs": len(unfinished),
    }
