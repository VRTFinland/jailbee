"""Container host-status commands through a real local broker socket."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from tests.test_inbox_socket_cli import FakeBroker, broker, sockdir  # noqa: F401 - shared fixtures


STATUS = {
    "load1": 2.5, "cpus": 8, "memory_total": 17179869184,
    "memory_available": 6442450944, "storage_pool": "work", "disk_available": 10737418240,
    "running_containers": 3,
}


@pytest.mark.parametrize("as_json", [False, True])
def test_host_status_output(broker: FakeBroker, sockdir, monkeypatch, as_json: bool) -> None:
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "broker.sock"))
    broker.on("host.status", STATUS)
    result = CliRunner().invoke(container_app, ["host", "status", *(["--json"] if as_json else [])])
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.output) == STATUS
    else:
        for text in ("2.50", "8 CPUs", "16.0 GiB", "6.0 GiB", "10.0 GiB", "work", "3"):
            assert text in result.output
    assert [(r.op, r.args) for r in broker.requests] == [("host.status", {})]


def test_host_status_retries_missing_socket_until_broker_starts(sockdir, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "broker.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "2")
    started = []

    def start() -> None:
        time.sleep(0.1)
        server = FakeBroker(sockdir / "broker.sock")
        server.on("host.status", STATUS)
        started.append(server)

    worker = threading.Thread(target=start)
    worker.start()
    try:
        result = CliRunner().invoke(container_app, ["host", "status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == STATUS
    finally:
        worker.join(timeout=3)
        for server in started:
            server.close()


def test_host_status_avoids_host_dependencies(broker: FakeBroker, sockdir) -> None:
    broker.on("host.status", STATUS)
    script = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pydantic', 'yaml', 'sqlmodel'} or fullname == 'jailbee.config':
            raise AssertionError('host import: ' + fullname)
sys.meta_path.insert(0, Guard())
from jailbee.container_cli import container_app
container_app(args=['host', 'status', '--json'], prog_name='jailbee')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "JAILBEE_ORCHESTRATION_SOCKET": str(sockdir / "broker.sock")},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == STATUS


def test_host_status_broker_down_retries_then_exits_four(sockdir, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "missing.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0.01")
    result = CliRunner().invoke(container_app, ["host", "status"])
    assert result.exit_code == 4
    assert "host broker is not reachable" in result.output
