from converge_orchestrator import cli
from converge_orchestrator.graph_service import build_graph as service_build_graph


def test_cli_uses_canonical_durable_service_graph() -> None:
    assert cli.build_graph is service_build_graph
