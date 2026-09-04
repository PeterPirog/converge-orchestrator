from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any, Protocol


class RegistryReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...

    def runs_for_project(self, project_id: str) -> list[dict[str, Any]]: ...


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def collect_registry_snapshot(
    registry: RegistryReader,
    persistence_backend: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return low-cardinality operational state derived only from durable registry records."""
    observed_at = _as_utc(now) if now is not None else datetime.now(UTC)
    if observed_at is None:
        raise ValueError("now must be a valid datetime")
    projects = registry.list_projects()
    runs = [run for project in projects for run in registry.runs_for_project(str(project["id"]))]

    status_counts = Counter(str(run.get("status") or "unknown") for run in runs)
    unfinished = [run for run in runs if not run.get("finished_at")]
    lease_counts: Counter[str] = Counter()
    timestamp_parse_issues = 0
    oldest_unfinished_age = 0.0

    for run in unfinished:
        lease_owner = run.get("lease_owner")
        raw_expiry = run.get("lease_expires_at")
        expiry = _as_utc(raw_expiry)
        if raw_expiry and expiry is None:
            timestamp_parse_issues += 1

        if lease_owner:
            if expiry is None:
                lease_counts["malformed"] += 1
            elif expiry > observed_at:
                lease_counts["active"] += 1
            else:
                lease_counts["expired"] += 1
        elif raw_expiry:
            lease_counts["malformed"] += 1
        else:
            lease_counts["none"] += 1

        started = _as_utc(run.get("started_at"))
        if started is None:
            timestamp_parse_issues += 1
        else:
            oldest_unfinished_age = max(
                oldest_unfinished_age,
                max(0.0, (observed_at - started).total_seconds()),
            )

    snapshot_counts: Counter[str] = Counter()
    for run in runs:
        path = run.get("config_snapshot_path")
        digest = run.get("config_snapshot_sha256")
        if path and digest:
            snapshot_counts["pinned"] += 1
        elif not path and not digest:
            snapshot_counts["legacy_unpinned"] += 1
        else:
            snapshot_counts["incomplete"] += 1

    affinity_counts: Counter[str] = Counter()
    for project in projects:
        workspace = bool(project.get("workspace_id"))
        state_store = bool(project.get("state_store_id"))
        if workspace and state_store:
            affinity_counts["complete"] += 1
        elif workspace or state_store:
            affinity_counts["incomplete"] += 1
        else:
            affinity_counts["legacy_unbound"] += 1

    return {
        "schema_version": 1,
        "generated_at": observed_at.isoformat(),
        "persistence_backend": persistence_backend,
        "projects": len(projects),
        "project_affinity": dict(sorted(affinity_counts.items())),
        "runs": len(runs),
        "runs_unfinished": len(unfinished),
        "runs_by_status": dict(sorted(status_counts.items())),
        "runs_with_error": sum(bool(run.get("error")) for run in runs),
        "leases": dict(sorted(lease_counts.items())),
        "config_snapshots": dict(sorted(snapshot_counts.items())),
        "oldest_unfinished_age_seconds": round(oldest_unfinished_age, 3),
        "timestamp_parse_issues": timestamp_parse_issues,
    }


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(lines: list[str], name: str, help_text: str, value: int | float) -> None:
    lines.extend(
        [
            f"# HELP {name} {help_text}",
            f"# TYPE {name} gauge",
            f"{name} {value}",
        ]
    )


def render_prometheus(snapshot: dict[str, Any]) -> str:
    """Render a Prometheus 0.0.4 text snapshot without high-cardinality identifiers."""
    lines: list[str] = []
    backend = _label(str(snapshot["persistence_backend"]))
    lines.extend(
        [
            "# HELP converge_persistence_backend_info Active durable persistence backend.",
            "# TYPE converge_persistence_backend_info gauge",
            f'converge_persistence_backend_info{{backend="{backend}"}} 1',
        ]
    )
    _metric(lines, "converge_projects", "Registered projects.", int(snapshot["projects"]))
    _metric(lines, "converge_runs", "Durable runs in the registry.", int(snapshot["runs"]))
    _metric(
        lines,
        "converge_runs_unfinished",
        "Runs without a terminal registry timestamp.",
        int(snapshot["runs_unfinished"]),
    )
    _metric(
        lines,
        "converge_runs_with_error",
        "Durable runs carrying a recorded error.",
        int(snapshot["runs_with_error"]),
    )
    _metric(
        lines,
        "converge_oldest_unfinished_age_seconds",
        "Age of the oldest unfinished run.",
        float(snapshot["oldest_unfinished_age_seconds"]),
    )
    _metric(
        lines,
        "converge_registry_timestamp_parse_issues",
        "Malformed durable timestamps observed while building the snapshot.",
        int(snapshot["timestamp_parse_issues"]),
    )

    families = (
        ("runs_by_status", "converge_runs_by_status", "status"),
        ("leases", "converge_run_leases", "state"),
        ("config_snapshots", "converge_run_config_snapshots", "state"),
        ("project_affinity", "converge_project_affinity", "state"),
    )
    for source, metric_name, label_name in families:
        values = snapshot.get(source, {}) or {}
        lines.extend(
            [
                f"# HELP {metric_name} Current {source.replace('_', ' ')} counts.",
                f"# TYPE {metric_name} gauge",
            ]
        )
        for label_value, value in sorted(values.items()):
            escaped = _label(str(label_value))
            lines.append(f'{metric_name}{{{label_name}="{escaped}"}} {int(value)}')

    return "\n".join(lines) + "\n"
