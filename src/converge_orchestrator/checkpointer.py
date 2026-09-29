"""Converge-specific SqliteSaver with int version compatibility.

SqliteSaver.get_next_version returns string versions (format '{032d}.{016f}'),
but BaseCheckpointSaver returns int, and existing durable checkpoints have
int versions. This mismatch causes TypeError in get_new_channel_versions
during recovery when a channel is bumped and its new string version is
compared against existing int versions.

This subclass restores the base class contract (int versions) so that
recovery from existing checkpoints works correctly.
"""
from __future__ import annotations

from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver as UpstreamSqliteSaver


class SqliteSaver(UpstreamSqliteSaver):
    """Converge-compatible SqliteSaver that returns int versions."""

    def get_next_version(self, current: Any, channel: Any) -> int:
        """Generate the next integer version for a channel.

        Matches BaseCheckpointSaver contract: returns int, monotonically increasing.
        """
        if current is None:
            return 1
        if isinstance(current, int):
            return current + 1
        if isinstance(current, str):
            # Handle string versions from older checkpoints (format 'NNN.NNN')
            try:
                return int(current.split(".")[0]) + 1
            except (ValueError, IndexError):
                return 1
        return 1