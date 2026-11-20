import pytest


@pytest.fixture(autouse=True)
def no_docker_daemon(monkeypatch):
    """Unit tests never ask a real Docker daemon for its bridge gateway."""
    monkeypatch.setattr("multi_agent_harness.harbor.environment.docker_bridge_gateway", lambda: "172.18.0.1")
